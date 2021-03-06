#!/usr/bin/env python
# vim: set ts=4 sw=4 et:
#
# Copyright (c) 2013 - 2014 Intel Corporation
#
# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 2
# of the License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA  02110-1301, USA.
#
# AUTHORS
# Laurentiu Palcu   <laurentiu.palcu@intel.com>
# Marius Avram      <marius.avram@intel.com>
#

import os
import re
import sys
import logging as log
from logging import debug as D
from logging import info as I
from logging import warning as W

from errors import *
from utils.bitbake import *

def is_recipe_or_include_file(env, full_path_f, f):
    is_file = os.path.isfile(full_path_f)

    is_recipe = f.find(env['PN']) == 0 and \
                f.find(env['PKGV']) != -1 and \
                f.find(".bb") != -1

    is_include = f.find(env['PN']) == 0 and \
                 f.find(".inc") != -1

    return is_file and (is_recipe or is_include)

def modify_recipe_files(func):
    def modify(env, recipe_dir, *args, **kwargs):
        for f in os.listdir(recipe_dir):
            full_path_f = os.path.join(recipe_dir, f)
            if is_recipe_or_include_file(env, full_path_f, f):
                with open(full_path_f + ".tmp", "w+") as temp_recipe:
                    with open(full_path_f) as recipe:
                        for line in recipe:
                            func(line, temp_recipe, *args, **kwargs)
                os.rename(full_path_f + ".tmp", full_path_f)
    return modify

def read_recipe_files(func):
    def read(env, recipe_dir):
        for f in os.listdir(recipe_dir):
            full_path_f = os.path.join(recipe_dir, f)
            if is_recipe_or_include_file(env, full_path_f, f):
                with open(full_path_f) as recipe:
                    for line in recipe:
                        func(line)
    return read

class Recipe(object):
    def __init__(self, env, new_ver, interactive, workdir, recipe_dir, bitbake, git):
        self.env = env
        self.new_ver = new_ver
        self.interactive = interactive
        self.workdir = workdir
        self.recipe_dir = recipe_dir
        self.bb = bitbake
        self.bb.set_log_dir(workdir)
        self.git = git

        self.retried_recipes = set()
        self.license_diff_file = None

        self.recipes_renamed = False
        self.checksums_changed = False

        self.removed_patches = False

        self.suffixes = [
            "tar.gz", "tgz", "zip", "tar.bz2", "tar.xz", "tar.lz4", "bz2",
            "lz4", "orig.tar.gz", "src.tar.gz", "src.rpm", "src.tgz",
            "svnr\d+.tar.bz2", "stable.tar.gz", "src.rpm"]
        self.old_env = None

        self.commit_msg = self.env['PN'] + ": upgrade to " + self.new_ver + "\n\n"
        self.comment_patches_msg = "\n\nCommented the following patch(es):\n"

        self._inherits = None

        super(Recipe, self).__init__()

    def get_inherits(self):
        @read_recipe_files
        def _get_inherits(line):
            m = re.search("^inherit (.*)$", line)
            if m:
                tmp = m.group(1).split()
                self._inherits.extend(tmp)

        if not self._inherits:
            self._inherits = []
            _get_inherits(self.env, self.recipe_dir)

        return self._inherits

    def update_env(self, env):
        self.env = env

    def _rename_files_dir(self, old_ver, new_ver):
        # The files directory is renamed only if the previous
        # one has the following format PackageName-PackageVersion.
        # Otherwise is kept the same way.
        src_dir = os.path.join(self.recipe_dir, self.env['PN'] + "-" + old_ver)
        dest_dir = os.path.join(self.recipe_dir, self.env['PN'] + "-" + new_ver)

        if os.path.exists(src_dir) and os.path.isdir(src_dir):
            self.git.mv(src_dir, dest_dir)

    def rename(self):
        # clean PR before renaming
        @modify_recipe_files
        def _clean_pr(line, temp_recipe, *args, **kwargs):
            if not (line.startswith("PR=") or line.startswith("PR =")):
                temp_recipe.write(line)
        _clean_pr(self.env, self.recipe_dir)

        # rename recipes (not directories)
        for path in os.listdir(self.recipe_dir):
            full_path = os.path.join(self.recipe_dir, path)
            if os.path.isfile(full_path) \
              and path.find(self.env['PN']) == 0 \
              and path.find(self.env['PKGV']) != -1:
                new_path = re.sub(re.escape(self.env['PKGV']), self.new_ver, path)
                self.git.mv(os.path.join(self.recipe_dir, path),
                            os.path.join(self.recipe_dir, new_path))

        # rename files/PN-PV directories to PN
        self._rename_files_dir(self.env['PKGV'], self.new_ver)

        self.recipes_renamed = True

        # since we did some renaming, backup the current environment
        self.old_env = self.env

    def create_diff_file(self, file, old_md5, new_md5):
        old_file = os.path.join(self.old_env['S'], file)
        new_file = os.path.join(self.env['S'], file)
        cmd = "diff -Nup " + old_file + " " + new_file + " > " + \
              os.path.join(self.workdir, os.path.basename(file + ".diff"))

        try:
            stdout, stderr = bb.process.run(cmd)
        except bb.process.ExecutionError:
            pass

        with open(os.path.join(self.workdir, "license_checksums.txt"), "w+") as f:
            f.write("old checksum = %s\n" % old_md5)
            f.write("new_checksum = %s\n" % new_md5)

    def _change_recipe_checksums(self, fetch_log):
        sums = {}

        with open(os.path.realpath(fetch_log)) as log:
            for line in log:
                m = None
                key = None
                m1 = re.match("^SRC_URI\[(.*)md5sum\].*", line)
                m2 = re.match("^SRC_URI\[(.*)sha256sum\].*", line)
                if m1:
                    m = m1
                    key = "md5sum"
                elif m2:
                    m = m2
                    key = "sha256sum"

                if m:
                    name = m.group(1)
                    sum_line = m.group(0) + '\n'
                    if name not in sums:
                        sums[name] = {}
                    sums[name][key] = sum_line;

        if len(sums) == 0:
            raise FetchError()

        # checksums are usually in the main recipe but they can also be in inc
        # files... Go through the recipes/inc files until we find them
        @modify_recipe_files
        def _update_recipe_checksums(line, temp_recipe, *args, **kwargs):
            sums = args[0]
            for name in sums:
                m1 = re.match("^SRC_URI\["+ name + "md5sum\].*", line)
                m2 = re.match("^SRC_URI\["+ name + "sha256sum\].*", line)
                if m1:
                    temp_recipe.write(sums[name]["md5sum"])
                elif m2:
                    temp_recipe.write(sums[name]["sha256sum"])
                else:
                    temp_recipe.write(line)

        I(" %s: Update recipe checksums ..." % self.env['PN'])
        _update_recipe_checksums(self.env, self.recipe_dir, sums)

        self.checksums_changed = True

    def _is_uri_failure(self, fetch_log):
        uri_failure = None
        checksum_failure = None
        with open(os.path.realpath(fetch_log)) as log:
            for line in log:
                if not uri_failure:
                    uri_failure = re.match(".*Fetcher failure for URL.*", line)
                if not checksum_failure:
                    checksum_failure = re.match(".*Checksum mismatch.*", line)
        if uri_failure and not checksum_failure:
            return True
        else:
            return False


    def _change_source_suffix(self, new_suffix):
        # Will change the extension of the archive from the SRC_URI

        @modify_recipe_files
        def _change(line, temp_recipe, *args, **kwargs):
            d = args[0]

            # source on first line
            m1 = re.match("^SRC_URI.*\${PV}\.(.*)[\" \\\\].*", line)
            # SRC_URI alone on the first line
            m2 = re.match("^SRC_URI.*", line)
            # source on second line
            m3 = re.match(".*\${PV}\.(.*)[\" \\\\].*", line)
            if m1:
                old_suffix = m1.group(1)
                line = line.replace(old_suffix, new_suffix+" ")
            if m2 and not m1:
                d['source_found'] = True
            if m3 and d['source_found']:
                old_suffix = m3.group(1)
                line = line.replace(old_suffix, new_suffix+" ")
                d['source_found'] = False

            temp_recipe.write(line)

        d = {}
        d['source_found'] = False
        _change(self.env, self.recipe_dir, d)

    def _comment_patch_uri(self, uri):
        @modify_recipe_files
        def _comment(line, temp_recipe, *args, **kwargs):
            d = args[0]
            uri = d['uri']

            m1 = re.match("SRC_URI *\+*= *\" *" + uri + " *\"", line)
            m2 = re.match("(SRC_URI *\+*= *\" *)" + uri + " *\\\\", line)
            m3 = re.match("[\t ]*" + uri + " *\\\\", line)
            m4 = re.match("([\t ]*)" + uri + " *\"", line)

            if m1 or m2 or m3 or m4:
                d['commented'] = True

                if not d['start']:
                    d['start'] = True

                # patch on a single SRC_URI line:
                if m1:
                    d['patches'].append(line)
                    d['end'] = True
                # patch is on the first SRC_URI line
                elif m2:
                    d['patches'].append(line)
                    temp_recipe.write(m2.group(1) + "\\\n")
                # patch is in the middle
                elif m3:
                    d['patches'].append(line)
                # patch is last in list
                elif m4:
                    d['end'] = True
                    d['patches'].append(line)
                    temp_recipe.write(m4.group(1) + "\"\n")
            else:
                temp_recipe.write(line)

                if d['start'] and (d['end'] or line.strip().startswith('"')):
                    if d['patches']:
                        for p in d['patches']:
                            line = p.rstrip()
                            if line.endswith('\\'):
                                line = line[:-1]
                            temp_recipe.write("#%s\n" % line)

                    d['start'] = False
                    d['end'] = False

        d = {}
        d['commented'] = False

        d['uri'] = uri
        d['patches'] = []
        d['start'] = False
        d['end'] = False
        _comment(self.env, self.recipe_dir, d)

        return d['commented']

    def _comment_faulty_patch(self, patch_log):
        patch_file = None
        is_reverse_applied = False

        with open(patch_log) as log:
            for line in log:
                m1 = re.match("^Patch ([^ ]*) does not apply.*", line)
                m2 = re.match("Patch ([^ ]*) can be reverse-applied", line)
                if m2:
                    m1 = m2
                    is_reverse_applied = True
                if m1:
                    patch_file = m1.group(1)
                    break

        if not patch_file:
            return False

        I(" %s: Commenting patch %s ..." % (self.env['PN'], patch_file))
        reason = None
        found = False
        dirs = [self.env['PN'] + "-" + self.env['PKGV'], self.env['PN'], "files"]
        for dir in dirs:
            patch_file_path = os.path.join(self.recipe_dir, dir, patch_file)
            if not os.path.exists(patch_file_path):
                continue
            else:
                found = True

                # Find out upstream status of the patch
                with open(patch_file_path) as patch:
                    for line in patch:
                        m = re.match(".*Upstream-Status:(.*)\n", line)
                        if m:
                            reason = m.group(1).strip().split()[0].lower()

                if not self._comment_patch_uri("file://" + patch_file):
                    return False
        if not found:
            return False

        self.comment_patches_msg += " * " + patch_file
        if reason:
            self.comment_patches_msg += " (" + reason + ") "
        if is_reverse_applied:
            self.comment_patches_msg += "+ reverse-applied"
        self.comment_patches_msg += "\n"

        return True

    def _is_license_issue(self, config_log):
        with open(config_log) as log:
            for line in log:
                m = re.match("ERROR: " + self.env['PN'] +
                             "[^:]*: md5 data is not matching for file", line)
                if m is not None:
                    return True

        return False

    def _license_issue_handled(self, config_log):
        @modify_recipe_files
        def _update_license_checksum(line, temp_recipe, *args, **kwargs):
            d = args[0]
            m = re.match("(.*)" + d['old_md5'] + "(.*)", line)
            if m is not None:
                temp_recipe.write(m.group(1) + d['new_md5'] + m.group(2) + "\n")
            else:
                temp_recipe.write(line)

        license_file = None
        with open(config_log) as log:
            for line in log:
                if not line.startswith("ERROR:"):
                    continue
                m_old = re.match("ERROR: " + self.env['PN'] +
                        "[^:]*: md5 data is not matching for file://([^;]*);md5=(.*)$", line)
                if not m_old:
                    m_old = re.match("ERROR: " + self.env['PN'] +
                            "[^:]*: md5 data is not matching for file://([^;]*);beginline=[0-9]*;endline=[0-9]*;md5=(.*)$", line)
                if not m_old:
                    m_old = re.match("ERROR: " + self.env['PN'] +
                            "[^:]*: md5 data is not matching for file://([^;]*);endline=[0-9]*;md5=(.*)$", line)
                if not m_old:
                    m_old = re.match("ERROR: " + self.env['PN'] +
                            "[^:]*: md5 data is not matching for file://([^;]*);beginline=[0-9]*;md5=(.*)$", line)
                m_new = re.match("ERROR: " + self.env['PN'] +
                        "[^:]*: The new md5 checksum is (.*)", line)
                if m_old:
                    license_file = m_old.group(1)
                    old_md5 = m_old.group(2)
                elif m_new:
                    new_md5 = m_new.group(1)
        
        if license_file is not None:
            d = {}
            d['old_md5'] = old_md5
            d['new_md5'] = new_md5
            _update_license_checksum(self.env, self.recipe_dir, d)

            self.create_diff_file(license_file, old_md5, new_md5)
            self.license_diff_file = os.path.join(self.workdir, os.path.basename(license_file + ".diff"))

            if self.interactive:
                W("  %s: license checksum failed for file %s. The recipe has"
                  "been updated! View diff? (Y/n)" % (self.env['PN'], license_file))
                answer = sys.stdin.readline().strip().upper()
                if answer == '' or answer == 'Y':
                    I(" ################ Licence file diff #################")
                    with open(self.license_diff_file) as diff:
                        I("%s" % diff.read())
                    I(" ####################################################")
                I(" Retry compilation? (Y/n)")
                answer = sys.stdin.readline().strip().upper()
                if answer == '' or answer == 'Y':
                    return True
            else:
                W(" %s: license checksum failed for file %s."
                  " The recipe has been updated! Diff file located at %s" %
                  (self.env['PN'], license_file, self.license_diff_file))
                I(" Recompiling ...")
                self.commit_msg += "License checksum changed for file " + license_file
                return True

        return False

    def get_license_diff_file_name(self):
        file_name = None
        if not self.license_diff_file is None:
            file_name = os.path.basename(self.license_diff_file)

        return file_name

    def _get_failed_recipes(self, output):
        failed_tasks = dict()
        machine = None

        for line in output.split("\n"):
            machine_match = re.match("MACHINE[\t ]+= *\"(.*)\"$", line)
            task_log_match = re.match("ERROR: Logfile of failure stored in: (.*/([^/]*)/[^/]*/temp/log\.(.*)\.[0-9]*)", line)
            # For some reason do_package is reported differently
            qa_issue_match = re.match("ERROR: QA Issue: ([^ :]*): (.*) not shipped", line)

            if task_log_match:
                failed_tasks[task_log_match.group(2)] = (task_log_match.group(3), task_log_match.group(1))
            elif qa_issue_match:
                # Improvise path to log file
                failed_tasks[qa_issue_match.group(1)] = ("do_package", self.bb.get_stdout_log())
            elif machine_match:
                machine = machine_match.group(1)

        # we didn't detect any failed tasks? then something else is wrong
        if len(failed_tasks) == 0:
            raise Error("could not detect failed task")

        return (machine, failed_tasks)

    def _is_incompatible_host(self, output):
        for line in output.split("\n"):
            incomp_host = re.match("ERROR: " + self.env['PN'] + " was skipped: incompatible with host (.*) \(.*$", line)

            if incomp_host is not None:
                return True

        return False

    def _add_not_shipped(self, package_log):
        files_not_shipped = False
        files = []
        occurences = []
        prefixes = {
          "/usr"            : "prefix",
          "/bin"            : "base_bindir",
          "/sbin"           : "base_sbindir",
          "/lib"            : "base_libdir",
          "/usr/share"      : "datadir",
          "/etc"            : "sysconfdir",
          "/var"            : "localstatedir",
          "/usr/share/info" : "infodir",
          "/usr/share/man"  : "mandir",
          "/usr/share/doc"  : "docdir",
          "/srv"            : "servicedir",
          "/usr/bin"        : "bindir",
          "/usr/sbin"       : "sbindir",
          "/usr/libexec"    : "libexecdir",
          "/usr/lib"        : "libdir",
          "/usr/include"    : "includedir",
          "/usr/lib/opie"   : "palmtopdir",
          "/usr/lib/opie"   : "palmqtdir",
        }

        with open(package_log) as log:
            for line in log:
                if re.match(".*Files/directories were installed but not shipped.*", line):
                    I(" %s: Add new files in recipe ..." %  self.env['PN'])
                    files_not_shipped = True
                # Extract path
                line = line.strip()
                if line:
                    line = line.split()[0]
                if files_not_shipped and os.path.isabs(line):
                    # Count occurences for globbing
                    path_exists = False
                    for i in range(0, len(files)):
                        if line.find(files[i]) == 0:
                            path_exists = True
                            occurences[i] += 1
                            break
                    if not path_exists:
                        files.append(line)
                        occurences.append(1)

        for i in range(0, len(files)):
            # Change paths to globbing expressions where is the case
            if occurences[i] > 1:
                files[i] += "/*"
            largest_prefix = ""
            # Substitute prefix
            for prefix in prefixes:
                if files[i].find(prefix) == 0 and len(prefix) > len(largest_prefix):
                    largest_prefix = prefix
            if largest_prefix:
                replacement = "${" + prefixes[largest_prefix] + "}"
                files[i] = files[i].replace(largest_prefix, replacement)

        @modify_recipe_files
        def _append_new_files(line, temp_recipe, *args, **kwargs):
            d = args[0]

            if re.match("^FILES_\${PN}[ +=].*", line):
                d['files_clause'] = True
                temp_recipe.write(line)
                return

            # Get front spacing
            if d['files_clause']:
                front_spacing = re.sub("[^ \t]", "", line)

            # Append once the last line has of FILES has been reached
            if re.match(".*\".*", line) and d['files_clause']:
                d['files_clause'] = False
                line = line.replace("\"", "")
                line = line.rstrip()
                front_spacing = re.sub("[^ \t]", "", line)

                # Do not write an empty line
                if line.strip():
                    temp_recipe.write(line + " \\\n")

                # Add spacing in case there was none
                if len(front_spacing) == 0:
                    front_spacing = " " * 8

                # Write to file
                for i in range(len(files) - 1):
                    line = front_spacing + files[i] + " \\\n"
                    temp_recipe.write(line)

                line = front_spacing + files[len(files) - 1] + "\"\n"
                temp_recipe.write(line)
                return

            temp_recipe.write(line)

        d = {}
        d['files_clause'] = False
        _append_new_files(self.env, self.recipe_dir, d)

        return files_not_shipped

    def unpack(self):
        self.bb.unpack(self.env['PN'])

    def fetch(self):
        from recipe.git import GitRecipe

        def _try_fetch():
            try:
                self.bb.fetch(self.env['PN'])
                return
            except Error as e:
                machine, failed_recipes = self._get_failed_recipes(e.stdout)
                if not self.env['PN'] in failed_recipes:
                    raise Error("Unknown error occured during fetch",
                            stdout = e.stdout, stderr = e.stderr)

                fetch_log = failed_recipes[self.env['PN']][1]

                if not self._is_uri_failure(fetch_log) and not \
                        self.checksums_changed:
                    self._change_recipe_checksums(fetch_log)
                    self.checksums_changed = True
                    return True

                return False

        succeed = _try_fetch()

        if not succeed and not isinstance(self, GitRecipe):
            for sfx in self.suffixes:
                I(" Trying new SRC_URI suffix: %s ..." % sfx)
                self._change_source_suffix(sfx)

                succeed = _try_fetch()
                if succeed:
                    break

        if not succeed:
            raise Error("Can't built a valid SRC_URI")
        elif self.recipes_renamed and not self.checksums_changed:
            raise Error("Fetch succeeded without changing checksums")

    def cleanall(self):
        self.bb.cleanall(self.env['PN'])

    def _clean_failed_recipes(self, failed_recipes):
        already_retried = False
        for recipe in failed_recipes:
            if recipe in self.retried_recipes:
                # we already retried, we'd best leave it to a human to handle
                # it :)
                already_retried = True
            # put the recipe in the retried list
            self.retried_recipes.add(recipe)

        if already_retried:
            return False
        else:
            I(" %s: The following recipe(s): %s, failed.  "
              "Doing a 'cleansstate' and then retry ..." %
              (self.env['PN'], ' '.join(failed_recipes.keys())))

            self.bb.cleansstate(' '.join(failed_recipes.keys()))
            return True

    def _undo_temporary(self):
        # Undo removed patches
        if self.removed_patches:
            self.git.checkout_branch("upgrades")
            self.git.delete_branch("comment_patches")
            self.git.reset_hard()
            self.git.reset_soft(1)
            self.removed_patches = False

    def compile(self, machine):
        try:
            self.bb.complete(self.env['PN'], machine)
            if self.removed_patches:
                # move temporary changes into upgrades branch
                self.git.checkout_branch("upgrades")
                self.git.delete_branch("comment_patches")
                self.git.reset_soft(1)
                self.commit_msg += self.comment_patches_msg + "\n"
                self.removed_patches = False
        except Error as e:
            if self._is_incompatible_host(e.stdout):
                W(" %s: compilation failed: incompatible host" % self.env['PN'])
                return
            machine, failed_recipes = self._get_failed_recipes(e.stdout)
            if not self.env['PN'] in failed_recipes:
                if not self._clean_failed_recipes(failed_recipes):
                    self._undo_temporary()
                    raise CompilationError()

                # retry
                self.compile(machine)
            else:
                failed_task = failed_recipes[self.env['PN']][0]
                log_file = failed_recipes[self.env['PN']][1]
                if failed_task == "do_patch":

                    # Comment one patch after the other until
                    # compilation works.
                    if not self.removed_patches:
                        self.git.commit("temporary")
                        self.git.create_branch("comment_patches")
                        self.git.checkout_branch("comment_patches")
                        self.removed_patches = True

                    if not self._comment_faulty_patch(log_file):
                        self._undo_temporary()
                        raise PatchError()

                    # retry
                    I(" %s: Recompiling for %s ..." % (self.env['PN'], machine))
                    self.compile(machine)
                elif failed_task == "do_configure":
                    self._undo_temporary()
                    if not self._is_license_issue(log_file):
                        raise ConfigureError()

                    if not self._license_issue_handled(log_file):
                        raise LicenseError()
                    #retry
                    self.compile(machine)
                elif failed_task == "do_fetch":
                    raise FetchError()
                elif failed_task == "do_package":
                    raise PackageError()
                    #if self._add_not_shipped(log_file):
                    #    self.compile(machine)
                    #else:
                else:
                    self._undo_temporary()
                    # throw a compilation exception for everything else. It
                    # doesn't really matter
                    raise CompilationError()
