# cerbero - a multi-platform build system for Open Source software
# Copyright (C) 2012 Andoni Morales Alastruey <ylatuya@gmail.com>
#
# This library is free software; you can redistribute it and/or
# modify it under the terms of the GNU Library General Public
# License as published by the Free Software Foundation; either
# version 2 of the License, or (at your option) any later version.
#
# This library is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Library General Public License for more details.
#
# You should have received a copy of the GNU Library General Public
# License along with this library; if not, write to the
# Free Software Foundation, Inc., 59 Temple Place - Suite 330,
# Boston, MA 02111-1307, USA.

import os
import shutil

from cerbero.config import Platform
from cerbero.utils import git, svn, shell, _
from cerbero.errors import FatalError, InvalidRecipeError
import cerbero.utils.messages as m


class Source (object):
    '''
    Base class for sources handlers

    @ivar recipe: the parent recipe
    @type recipe: L{cerbero.recipe.Recipe}
    @ivar config: cerbero's configuration
    @type config: L{cerbero.config.Config}
    @cvar patches: list of patches to apply
    @type patches: list
    @cvar strip: number passed to the --strip 'patch' option
    @type patches: int
    '''

    patches = []
    strip = 1

    def fetch(self):
        '''
        Fetch the sources
        '''
        raise NotImplemented("'fetch' must be implemented by subclasses")

    def extract(self):
        '''
        Extracts the sources
        '''
        raise NotImplemented("'extract' must be implemented by subclasses")

    def replace_name_and_version(self, string):
        '''
        Replaces name and version in strings
        '''
        return string % {'name': self.name, 'version': self.version}


class CustomSource (Source):

    def fetch(self):
        pass

    def extract(self):
        pass


class Tarball (Source):
    '''
    Source handler for tarballs

    @cvar url: dowload URL for the tarball
    @type url: str
    '''

    url = None
    tarball_name = None
    tarball_dirname = None

    def __init__(self):
        Source.__init__(self)
        if not self.url:
            raise InvalidRecipeError(
                _("'url' attribute is missing in the recipe"))
        self.url = self.replace_name_and_version(self.url)
        if self.tarball_name is not None:
            self.tarball_name = \
                self.replace_name_and_version(self.tarball_name)
        else:
            self.tarball_name = os.path.basename(self.url)
        if self.tarball_dirname is not None:
            self.tarball_dirname = \
                self.replace_name_and_version(self.tarball_dirname)
        self.download_path = os.path.join(self.repo_dir, self.tarball_name)

    def fetch(self):
        m.action(_('Fetching tarball %s to %s') %
                 (self.url, self.download_path))
        if not os.path.exists(self.repo_dir):
            os.makedirs(self.repo_dir)
        shell.download(self.url, self.download_path, check_cert=False)

    def extract(self):
        m.action(_('Extracting tarball to %s') % self.build_dir)
        if os.path.exists(self.build_dir):
            shutil.rmtree(self.build_dir)
        shell.unpack(self.download_path, self.config.sources)
        if self.tarball_dirname is not None:
            os.rename(os.path.join(self.config.sources, self.tarball_dirname),
                    self.build_dir)
        git.init_directory(self.build_dir)
        for patch in self.patches:
            if not os.path.isabs(patch):
                patch = self.relative_path(patch)
            if self.strip == 1:
                git.apply_patch(patch, self.build_dir)
            else:
                shell.apply_patch(patch, self.build_dir, self.strip)


class GitCache (Source):
    '''
    Base class for source handlers using a Git repository
    '''

    checkout_rev = "cerbero-checkout"
    remotes = None
    commit = None

    def __init__(self):
        Source.__init__(self)
        if self.remotes is None:
            self.remotes = {}
        if not 'origin' in self.remotes:
            self.remotes['origin'] = '%s/%s.git' % \
                                     (self.config.git_root, self.name)
        self.repo_dir = os.path.join(self.config.local_sources, self.name)

    def _get_checkout_recipe_rev(self):
        # when we fetch a git repo we store the recipe revision in
        # .git/refs/cerbero-checkout. We then use it as an hint when we
        # fetch again to know whether it's safe to rewind history if necessary
        checkout_recipe_rev = git.read_ref(self.repo_dir, self.checkout_rev)
        if checkout_recipe_rev is None:
            m.warning(_("refs/%s does not exist") % self.checkout_rev)
        else:
            # sanity check
            if not git.revision_is_ancestor(self.repo_dir,
                    checkout_recipe_rev, "HEAD"):
                m.warning(_("refs/%s seems out of sync "
                        "with the current checkout, ignoring it")
                        % self.checkout_rev)
                checkout_recipe_rev = None
        return checkout_recipe_rev

    def _checkout(self, new_repo):
        commit = self.config.recipe_commit(self.name) or self.commit
        if new_repo:
            m.action(_("Checking out git repository %s at revision %s")
                    % (self.repo_dir, commit))
            # new repo, we can safely checkout
            git.checkout(self.repo_dir, commit)
            return commit

        m.action(_("Updating git repository %s to revision %s")
                % (self.repo_dir, commit))
        head = git.get_hash(self.repo_dir, "HEAD")
        if commit == head:
            # we're already at the target revision
            return commit

        # get the revision we checked out during the last fetch, if known
        checkout_recipe_rev = self._get_checkout_recipe_rev()
        if checkout_recipe_rev is not None:
            m.message(_("Current checkout recipe revision %s, HEAD %s")
                    % (checkout_recipe_rev, head))
            # were new revisions committed after the last fetch? If not, we're
            # going to git reset --hard. 
            have_local_commits = len(git.rev_list(self.repo_dir, "%s.." %
                    checkout_recipe_rev)) > 0
            moving_forward = git.revision_is_ancestor(self.repo_dir,
                    checkout_recipe_rev, commit)
        else:
            # we don't know for sure, so assume there might be unpushed
            # revisions. Better safe than sorry.
            have_local_commits = True
            moving_forward = git.revision_is_ancestor(self.repo_dir,
                    'HEAD', commit)

        if not have_local_commits:
            # we're absolutely sure that there are no commits on top of the last
            # recipe revision we fetched, we can safely git reset --hard here.
            m.message(_("Repository has no local commits, "
                "checking out revision %s") % commit)
            git.checkout(self.repo_dir, commit)
            return commit

        if moving_forward:
            # at this point we have local commits, we're moving forward in
            # history so we try to rebase
            try:
                git.rebase(self.repo_dir, commit)
            except FatalError, e:
                git.rebase_abort(self.repo_dir)
                raise e
            return commit
        elif self.config.fetch_resets_repository:
            # we have local commits and are checking out an older revision than
            # what the local commits are based on. This is a dangerous operation
            # so we only do it if fetch_resets_repository=True in the conf
            m.warning(_("Rebase not possible. "
                "Potentially discarding commits since "
                "fetch_resets_repository is set to True. (Was at %s)") % head)
            git.checkout(self.repo_dir, commit)
            return commit

        # we couldn't update the repo :(
        if have_local_commits:
            m.warning(_("The repository contains local commits which "
                "can't be easily rebased on top of the new recipe revision"))
        else:
            m.warning(_("Couldn't figure out whether updating to the new "
                "revision would lose history. Bailing since fetch_resets_repository "
                "is set to False"))

        raise FatalError(_("Could not update repository %s to revision %s")
                % (self.repo_dir, commit))

    def fetch(self, checkout=True):
        if not os.path.exists(self.repo_dir):
            git.init(self.repo_dir)
            new_repo = True
        else:
            new_repo = False
        for remote, url in self.remotes.iteritems():
            git.add_remote(self.repo_dir, remote, url)
        # fetch remote branches
        git.fetch(self.repo_dir, fail=False)
        if checkout:
            commit = self._checkout(new_repo)
            git.write_ref(self.repo_dir, self.checkout_rev, commit)
            m.message(_("Checked out revision %s") % commit)

    def built_version(self):
        return '%s+git~%s' % (self.version, git.get_hash(self.repo_dir, self.commit))


class LocalTarball (GitCache):
    '''
    Source handler for cerbero's local sources, a local git repository with
    the release tarball and a set of patches
    '''

    BRANCH_PREFIX = 'sdk'

    def __init__(self):
        GitCache.__init__(self)
        self.commit = "%s/%s-%s" % ('origin',
                                    self.BRANCH_PREFIX, self.version)
        self.platform_patches_dir = os.path.join(self.repo_dir,
                                                 self.config.platform)
        self.package_name = self.package_name
        self.unpack_dir = self.config.sources

    def extract(self):
        if not os.path.exists(self.build_dir):
            os.mkdir(self.build_dir)
        self._find_tarball()
        shell.unpack(self.tarball_path, self.unpack_dir)
        # apply common patches
        self._apply_patches(self.repo_dir)
        # apply platform patches
        self._apply_patches(self.platform_patches_dir)

    def _find_tarball(self):
        tarball = [x for x in os.listdir(self.repo_dir) if
                   x.startswith(self.package_name)]
        if len(tarball) != 1:
            raise FatalError(_("The local repository %s do not have a "
                             "valid tarball") % self.repo_dir)
        self.tarball_path = os.path.join(self.repo_dir, tarball[0])

    def _apply_patches(self, patches_dir):
        if not os.path.isdir(patches_dir):
            # FIXME: Add logs
            return

        # list patches in this directory
        patches = [os.path.join(patches_dir, x) for x in
                   os.listdir(patches_dir) if x.endswith('.patch')]
        # apply patches
        for patch in patches:
            shell.apply_patch(self.build_dir, patch)


class Git (GitCache):
    '''
    Source handler for git repositories
    '''

    def __init__(self):
        GitCache.__init__(self)
        if self.commit is None:
            self.commit = 'origin/sdk-%s' % self.version
        # For forced commits in the config
        self.commit = self.config.recipe_commit(self.name) or self.commit

    def extract(self):
        if os.path.exists(self.build_dir):
            # fix read-only permissions
            if self.config.platform == Platform.WINDOWS:
                shell.call('chmod -R +w .git/', self.build_dir, fail=False)
            try:
                commit_hash = git.get_hash(self.repo_dir, self.commit)
                checkout_hash = git.get_hash(self.build_dir, 'HEAD')
                if commit_hash == checkout_hash and not self.patches:
                    return False
            except Exception:
                pass
            shutil.rmtree(self.build_dir)
        if not os.path.exists(self.build_dir):
            os.mkdir(self.build_dir)

        m.action(_("Extracting to %s") % self.build_dir)
        # checkout the current version
        git.local_checkout(self.build_dir, self.repo_dir, self.commit)

        for patch in self.patches:
            if not os.path.isabs(patch):
                patch = self.relative_path(patch)

            if self.strip == 1:
                git.apply_patch(patch, self.build_dir)
            else:
                shell.apply_patch(patch, self.build_dir, self.strip)

        return True


class GitExtractedTarball(Git):
    '''
    Source handle for git repositories with an extracted tarball

    Git doesn't conserve timestamps, which are reset after clonning the repo.
    This can confuse the autotools build system, producing innecessary calls
    to autoconf, aclocal, autoheaders or automake.
    For instance after doing './configure && make', 'configure' is called
    again if 'configure.ac' is newer than 'configure'.
    '''

    matches = ['.m4', '.in', 'configure']
    _files = {}

    def extract(self):
        if not Git.extract(self):
            return False
        for match in self.matches:
            self._files[match] = []
        self._find_files(self.build_dir)
        self._files['.in'] = [x for x in self._files['.in'] if
                os.path.join(self.build_dir, 'm4') not in x]
        self._fix_ts()

    def _fix_ts(self):
        for match in self.matches:
            for path in self._files[match]:
                shell.touch(path)

    def _find_files(self, directory):
        for path in os.listdir(directory):
            full_path = os.path.join(directory, path)
            if os.path.isdir(full_path):
                self._find_files(full_path)
            if path == 'configure.in':
                continue
            for match in self.matches:
                if path.endswith(match):
                    self._files[match].append(full_path)


class Svn(Source):
    '''
    Source handler for svn repositories
    '''

    url = None
    revision = 'HEAD'

    def __init__(self):
        Source.__init__(self)
        # For forced revision in the config
        self.revision = self.config.recipe_commit(self.name) or self.revision

    def fetch(self):
        if os.path.exists(self.repo_dir):
            shutil.rmtree(self.repo_dir)
        os.makedirs(self.repo_dir)
        svn.checkout(self.url, self.repo_dir)
        svn.update(self.repo_dir, self.revision)

    def extract(self):
        if os.path.exists(self.build_dir):
            shutil.rmtree(self.build_dir)

        shutil.copytree(self.repo_dir, self.build_dir)

    def built_version(self):
        return '%s+svn~%s' % (self.version, svn.revision(self.repo_dir))


class SourceType (object):

    CUSTOM = CustomSource
    TARBALL = Tarball
    LOCAL_TARBALL = LocalTarball
    GIT = Git
    GIT_TARBALL = GitExtractedTarball
    SVN = Svn
