# -*- coding: utf-8 -*-
# Copyright (C) 2012 Anaconda, Inc
# SPDX-License-Identifier: BSD-3-Clause
from __future__ import absolute_import, division, print_function, unicode_literals

from abc import ABCMeta, abstractmethod, abstractproperty
from json import JSONDecodeError
from logging import getLogger
from os.path import basename, dirname, getsize, isdir, join
import re
import sys
from uuid import uuid4

try:
    from tlz.itertoolz import concat
except ImportError:
    from conda._vendor.toolz.itertoolz import concat

from .envs_manager import get_user_environments_txt_file, register_env, unregister_env
from .portability import _PaddingError, update_prefix
from .prefix_data import PrefixData
from .. import CondaError
from ..auxlib.ish import dals
from ..base.constants import CONDA_TEMP_EXTENSION
from ..base.context import context
from ..common.compat import on_win
from ..common.path import (get_bin_directory_short_path, get_leaf_directories,
                           get_python_noarch_target_path, get_python_short_path,
                           parse_entry_point_def,
                           pyc_path, url_to_path, win_path_ok)
from ..common.url import has_platform, path_to_url
from ..exceptions import (CondaUpgradeError, CondaVerificationError, NotWritableError,
                          PaddingError, SafetyError)
from ..gateways.connection.download import download
from ..gateways.disk.create import (compile_multiple_pyc, copy,
                                    create_hard_link_or_copy, create_link,
                                    create_python_entry_point, extract_tarball,
                                    make_menu, mkdir_p, write_as_json_to_file)
from ..gateways.disk.delete import rm_rf
from ..gateways.disk.permissions import make_writable
from ..gateways.disk.read import (compute_md5sum, compute_sha256sum, islink, lexists,
                                  read_index_json)
from ..gateways.disk.update import backoff_rename, touch
from ..history import History
from ..models.channel import Channel
from ..models.enums import LinkType, NoarchType, PathType
from ..models.match_spec import MatchSpec
from ..models.records import (Link, PackageCacheRecord, PackageRecord, PathDataV1, PathsData,
                              PrefixRecord)

try:
    FileNotFoundError
except NameError:
    FileNotFoundError = IOError

log = getLogger(__name__)

REPR_IGNORE_KWARGS = (
    'transaction_context',
    'package_info',
    'hold_path',
)

class PathAction(metaclass=ABCMeta):

    _verified = False

    @abstractmethod
    def verify(self):
        # if verify fails, it should return an exception object rather than raise
        #  at the end of a verification run, all errors will be raised as a CondaMultiError
        # after successful verification, the verify method should set self._verified = True
        raise NotImplementedError()

    @abstractmethod
    def execute(self):
        raise NotImplementedError()

    @abstractmethod
    def reverse(self):
        raise NotImplementedError()

    @abstractmethod
    def cleanup(self):
        raise NotImplementedError()

    @abstractproperty
    def target_full_path(self):
        raise NotImplementedError()

    @property
    def verified(self):
        return self._verified

    def __repr__(self):
        args = ('%s=%r' % (key, value) for key, value in vars(self).items()
                if key not in REPR_IGNORE_KWARGS)
        return "%s(%s)" % (self.__class__.__name__, ', '.join(args))


class MultiPathAction(metaclass=ABCMeta):

    _verified = False

    @abstractmethod
    def verify(self):
        # if verify fails, it should return an exception object rather than raise
        #  at the end of a verification run, all errors will be raised as a CondaMultiError
        # after successful verification, the verify method should set self._verified = True
        raise NotImplementedError()

    @abstractmethod
    def execute(self):
        raise NotImplementedError()

    @abstractmethod
    def reverse(self):
        raise NotImplementedError()

    @abstractmethod
    def cleanup(self):
        raise NotImplementedError()

    @abstractproperty
    def target_full_paths(self):
        raise NotImplementedError()

    @property
    def verified(self):
        return self._verified

    def __repr__(self):
        args = ('%s=%r' % (key, value) for key, value in vars(self).items()
                if key not in REPR_IGNORE_KWARGS)
        return "%s(%s)" % (self.__class__.__name__, ', '.join(args))


class PrefixPathAction(PathAction, metaclass=ABCMeta):

    def __init__(self, transaction_context, target_prefix, target_short_path):
        self.transaction_context = transaction_context
        self.target_prefix = target_prefix
        self.target_short_path = target_short_path

    @property
    def target_short_paths(self):
        return (self.target_short_path,)

    @property
    def target_full_path(self):
        trgt, shrt_pth = self.target_prefix, self.target_short_path
        if trgt is not None and shrt_pth is not None:
            return join(trgt, win_path_ok(shrt_pth))
        else:
            return None


# ######################################################
#  Creation of Paths within a Prefix
# ######################################################

class CreateInPrefixPathAction(PrefixPathAction, metaclass=ABCMeta):
    # All CreatePathAction subclasses must create a SINGLE new path
    #   the short/in-prefix version of that path must be returned by execute()

    def __init__(self, transaction_context, package_info, source_prefix, source_short_path,
                 target_prefix, target_short_path):
        super(CreateInPrefixPathAction, self).__init__(transaction_context,
                                                       target_prefix, target_short_path)
        self.package_info = package_info
        self.source_prefix = source_prefix
        self.source_short_path = source_short_path

    def verify(self):
        self._verified = True

    def cleanup(self):
        # create actions typically won't need cleanup
        pass

    @property
    def source_full_path(self):
        prfx, shrt_pth = self.source_prefix, self.source_short_path
        return join(prfx, win_path_ok(shrt_pth)) if prfx and shrt_pth else None


class LinkPathAction(CreateInPrefixPathAction):

    @classmethod
    def create_file_link_actions(cls, transaction_context, package_info, target_prefix,
                                 requested_link_type):
        def get_prefix_replace(source_path_data):
            if source_path_data.path_type == PathType.softlink:
                link_type = LinkType.copy
                prefix_placehoder, file_mode = '', None
            elif source_path_data.prefix_placeholder:
                link_type = LinkType.copy
                prefix_placehoder = source_path_data.prefix_placeholder
                file_mode = source_path_data.file_mode
            elif source_path_data.no_link:
                link_type = LinkType.copy
                prefix_placehoder, file_mode = '', None
            else:
                link_type = requested_link_type
                prefix_placehoder, file_mode = '', None

            return link_type, prefix_placehoder, file_mode

        def make_file_link_action(source_path_data):
            # TODO: this inner function is still kind of a mess
            noarch = package_info.repodata_record.noarch
            if noarch is None and package_info.package_metadata is not None:
                # Look in package metadata in case it was omitted from repodata (see issue #8311)
                noarch = package_info.package_metadata.noarch
                if noarch is not None:
                    noarch = noarch.type
            if noarch == NoarchType.python:
                sp_dir = transaction_context['target_site_packages_short_path']
                if sp_dir is None:
                    raise CondaError("Unable to determine python site-packages "
                                     "dir in target_prefix!\nPlease make sure "
                                     "python is installed in %s" % target_prefix)
                target_short_path = get_python_noarch_target_path(source_path_data.path, sp_dir)
            elif noarch is None or noarch == NoarchType.generic:
                target_short_path = source_path_data.path
            else:
                raise CondaUpgradeError(dals("""
                The current version of conda is too old to install this package.
                Please update conda."""))

            link_type, placeholder, fmode = get_prefix_replace(source_path_data)

            if placeholder:
                return PrefixReplaceLinkAction(transaction_context, package_info,
                                               package_info.extracted_package_dir,
                                               source_path_data.path,
                                               target_prefix, target_short_path,
                                               requested_link_type,
                                               placeholder, fmode, source_path_data)
            else:
                return LinkPathAction(transaction_context, package_info,
                                      package_info.extracted_package_dir, source_path_data.path,
                                      target_prefix, target_short_path,
                                      link_type, source_path_data)
        return tuple(make_file_link_action(spi) for spi in package_info.paths_data.paths)

    @classmethod
    def create_directory_actions(cls, transaction_context, package_info, target_prefix,
                                 requested_link_type, file_link_actions):
        leaf_directories = get_leaf_directories(axn.target_short_path for axn in file_link_actions)
        return tuple(
            cls(transaction_context, package_info, None, None,
                target_prefix, directory_short_path, LinkType.directory, None)
            for directory_short_path in leaf_directories
        )

    @classmethod
    def create_python_entry_point_windows_exe_action(cls, transaction_context, package_info,
                                                     target_prefix, requested_link_type,
                                                     entry_point_def):
        source_directory = context.conda_prefix
        source_short_path = 'Scripts/conda.exe'
        command, _, _ = parse_entry_point_def(entry_point_def)
        target_short_path = "Scripts/%s.exe" % command
        source_path_data = PathDataV1(
            _path=target_short_path,
            path_type=PathType.windows_python_entry_point_exe,
        )
        return cls(transaction_context, package_info, source_directory,
                   source_short_path, target_prefix, target_short_path,
                   requested_link_type, source_path_data)

    def __init__(self, transaction_context, package_info,
                 extracted_package_dir, source_short_path,
                 target_prefix, target_short_path, link_type, source_path_data):
        super(LinkPathAction, self).__init__(transaction_context, package_info,
                                             extracted_package_dir, source_short_path,
                                             target_prefix, target_short_path)
        self.link_type = link_type
        self._execute_successful = False
        self.source_path_data = source_path_data
        self.prefix_path_data = None

    def verify(self):
        if self.link_type != LinkType.directory and not lexists(self.source_full_path):  # pragma: no cover  # NOQA
            return CondaVerificationError(dals("""
            The package for %s located at %s
            appears to be corrupted. The path '%s'
            specified in the package manifest cannot be found.
            """ % (self.package_info.repodata_record.name,
                   self.package_info.extracted_package_dir,
                   self.source_short_path)))

        source_path_data = self.source_path_data
        try:
            source_path_type = source_path_data.path_type
        except AttributeError:
            source_path_type = None
        if source_path_type in PathType.basic_types:
            # this let's us keep the non-generic path types like windows_python_entry_point_exe
            source_path_type = None

        if self.link_type == LinkType.directory:
            self.prefix_path_data = None
        elif self.link_type == LinkType.softlink:
            self.prefix_path_data = PathDataV1.from_objects(
                self.source_path_data,
                path_type=source_path_type or PathType.softlink,
            )
        elif self.link_type == LinkType.copy and source_path_data.path_type == PathType.softlink:
            self.prefix_path_data = PathDataV1.from_objects(
                self.source_path_data,
                path_type=source_path_type or PathType.softlink,
            )

        elif source_path_data.path_type == PathType.hardlink:
            try:
                reported_size_in_bytes = source_path_data.size_in_bytes
            except AttributeError:
                reported_size_in_bytes = None
            source_size_in_bytes = 0
            if reported_size_in_bytes:
                source_size_in_bytes = getsize(self.source_full_path)
                if reported_size_in_bytes != source_size_in_bytes:
                    return SafetyError(dals("""
                    The package for %s located at %s
                    appears to be corrupted. The path '%s'
                    has an incorrect size.
                      reported size: %s bytes
                      actual size: %s bytes
                    """ % (self.package_info.repodata_record.name,
                           self.package_info.extracted_package_dir,
                           self.source_short_path,
                           reported_size_in_bytes,
                           source_size_in_bytes,
                           )))

            try:
                reported_sha256 = source_path_data.sha256
            except AttributeError:
                reported_sha256 = None
            # sha256 is expensive.  Only run if file sizes agree, and then only if enabled
            if (source_size_in_bytes and reported_size_in_bytes == source_size_in_bytes
                    and context.extra_safety_checks):
                source_sha256 = compute_sha256sum(self.source_full_path)

                if reported_sha256 and reported_sha256 != source_sha256:
                    return SafetyError(dals("""
                    The package for %s located at %s
                    appears to be corrupted. The path '%s'
                    has a sha256 mismatch.
                    reported sha256: %s
                    actual sha256: %s
                    """ % (self.package_info.repodata_record.name,
                           self.package_info.extracted_package_dir,
                           self.source_short_path,
                           reported_sha256,
                           source_sha256,
                           )))
            self.prefix_path_data = PathDataV1.from_objects(
                source_path_data,
                sha256=reported_sha256,
                sha256_in_prefix=reported_sha256,
                path_type=source_path_type or PathType.hardlink,
            )
        elif source_path_data.path_type == PathType.windows_python_entry_point_exe:
            self.prefix_path_data = source_path_data
        else:
            raise NotImplementedError()

        self._verified = True

    def execute(self):
        log.trace("linking %s => %s", self.source_full_path, self.target_full_path)
        create_link(self.source_full_path, self.target_full_path, self.link_type,
                    force=context.force)
        self._execute_successful = True

    def reverse(self):
        if self._execute_successful:
            log.trace("reversing link creation %s", self.target_prefix)
            if not isdir(self.target_full_path):
                rm_rf(self.target_full_path, clean_empty_parents=True)


class PrefixReplaceLinkAction(LinkPathAction):

    def __init__(self, transaction_context, package_info,
                 extracted_package_dir, source_short_path,
                 target_prefix, target_short_path,
                 link_type,
                 prefix_placeholder, file_mode, source_path_data):
        # This link_type used in execute(). Make sure we always respect LinkType.copy request.
        link_type = LinkType.copy if link_type == LinkType.copy else LinkType.hardlink
        super(PrefixReplaceLinkAction, self).__init__(transaction_context, package_info,
                                                      extracted_package_dir, source_short_path,
                                                      target_prefix, target_short_path,
                                                      link_type, source_path_data)
        self.prefix_placeholder = prefix_placeholder
        self.file_mode = file_mode
        self.intermediate_path = None

    def verify(self):
        validation_error = super(PrefixReplaceLinkAction, self).verify()
        if validation_error:
            return validation_error

        if islink(self.source_full_path):
            log.trace("ignoring prefix update for symlink with source path %s",
                      self.source_full_path)
            # return
            assert False, "I don't think this is the right place to ignore this"

        mkdir_p(self.transaction_context['temp_dir'])
        self.intermediate_path = join(self.transaction_context['temp_dir'], str(uuid4()))

        log.trace("copying %s => %s", self.source_full_path, self.intermediate_path)
        create_link(self.source_full_path, self.intermediate_path, LinkType.copy)
        make_writable(self.intermediate_path)

        try:
            log.trace("rewriting prefixes in %s", self.target_full_path)
            update_prefix(self.intermediate_path,
                          context.target_prefix_override or self.target_prefix,
                          self.prefix_placeholder,
                          self.file_mode, subdir=self.package_info.repodata_record.subdir)
        except _PaddingError:
            raise PaddingError(self.target_full_path, self.prefix_placeholder,
                               len(self.prefix_placeholder))

        sha256_in_prefix = compute_sha256sum(self.intermediate_path)

        self.prefix_path_data = PathDataV1.from_objects(
            self.prefix_path_data,
            file_mode=self.file_mode,
            path_type=PathType.hardlink,
            prefix_placeholder=self.prefix_placeholder,
            sha256_in_prefix=sha256_in_prefix,
        )

        self._verified = True

    def execute(self):
        if not self._verified:
            self.verify()
        source_path = self.intermediate_path or self.source_full_path
        log.trace("linking %s => %s", source_path, self.target_full_path)
        create_link(source_path, self.target_full_path, self.link_type)
        self._execute_successful = True


class MakeMenuAction(CreateInPrefixPathAction):

    @classmethod
    def create_actions(cls, transaction_context, package_info, target_prefix, requested_link_type):
        if on_win and context.shortcuts:
            MENU_RE = re.compile(r'^menu/.*\.json$', re.IGNORECASE)
            return tuple(cls(transaction_context, package_info, target_prefix, spi.path)
                         for spi in package_info.paths_data.paths if bool(MENU_RE.match(spi.path)))
        else:
            return ()

    def __init__(self, transaction_context, package_info, target_prefix, target_short_path):
        super(MakeMenuAction, self).__init__(transaction_context, package_info,
                                             None, None, target_prefix, target_short_path)
        self._execute_successful = False

    def execute(self):
        log.trace("making menu for %s", self.target_full_path)
        make_menu(self.target_prefix, self.target_short_path, remove=False)
        self._execute_successful = True

    def reverse(self):
        if self._execute_successful:
            log.trace("removing menu for %s", self.target_full_path)
            make_menu(self.target_prefix, self.target_short_path, remove=True)


class CreateNonadminAction(CreateInPrefixPathAction):

    @classmethod
    def create_actions(cls, transaction_context, package_info, target_prefix, requested_link_type):
        if on_win and lexists(join(context.root_prefix, '.nonadmin')):
            return cls(transaction_context, package_info, target_prefix),
        else:
            return ()

    def __init__(self, transaction_context, package_info, target_prefix):
        super(CreateNonadminAction, self).__init__(transaction_context, package_info, None, None,
                                                   target_prefix, '.nonadmin')
        self._file_created = False

    def execute(self):
        log.trace("touching nonadmin %s", self.target_full_path)
        self._file_created = touch(self.target_full_path)

    def reverse(self):
        if self._file_created:
            log.trace("removing nonadmin file %s", self.target_full_path)
            rm_rf(self.target_full_path)


class CompileMultiPycAction(MultiPathAction):

    @classmethod
    def create_actions(cls, transaction_context, package_info, target_prefix, requested_link_type,
                       file_link_actions):
        noarch = package_info.package_metadata and package_info.package_metadata.noarch
        if noarch is not None and noarch.type == NoarchType.python:
            noarch_py_file_re = re.compile(r'^site-packages[/\\][^\t\n\r\f\v]+\.py$')
            py_ver = transaction_context['target_python_version']
            py_files = tuple((axn.target_short_path for axn in file_link_actions
                              if getattr(axn, 'source_short_path') and
                              noarch_py_file_re.match(axn.source_short_path)))
            pyc_files = tuple((pyc_path(pf, py_ver) for pf in py_files))
            return (cls(transaction_context, package_info, target_prefix, py_files, pyc_files), )
        else:
            return ()

    def __init__(self, transaction_context, package_info, target_prefix,
                 source_short_paths, target_short_paths):
        self.transaction_context = transaction_context
        self.package_info = package_info
        self.target_prefix = target_prefix
        self.source_short_paths = source_short_paths
        self.target_short_paths = target_short_paths
        self.prefix_path_data = None
        self.prefix_paths_data = [
            PathDataV1(_path=p, path_type=PathType.pyc_file,) for p in self.target_short_paths]
        self._execute_successful = False

    @property
    def target_full_paths(self):
        def join_or_none(prefix, short_path):
            if prefix is None or short_path is None:
                return None
            else:
                return join(prefix, win_path_ok(short_path))
        return (join_or_none(self.target_prefix, p) for p in self.target_short_paths)

    @property
    def source_full_paths(self):
        def join_or_none(prefix, short_path):
            if prefix is None or short_path is None:
                return None
            else:
                return join(prefix, win_path_ok(short_path))
        return (join_or_none(self.target_prefix, p) for p in self.source_short_paths)

    def verify(self):
        self._verified = True

    def cleanup(self):
        # create actions typically won't need cleanup
        pass

    def execute(self):
        # compile_pyc is sometimes expected to fail, for example a python 3.6 file
        #   installed into a python 2 environment, but no code paths actually importing it
        # technically then, this file should be removed from the manifest in conda-meta, but
        #   at the time of this writing that's not currently happening
        log.trace("compiling %s", ' '.join(self.target_full_paths))
        target_python_version = self.transaction_context['target_python_version']
        python_short_path = get_python_short_path(target_python_version)
        python_full_path = join(self.target_prefix, win_path_ok(python_short_path))
        compile_multiple_pyc(python_full_path, self.source_full_paths, self.target_full_paths,
                             self.target_prefix, self.transaction_context['target_python_version'])
        self._execute_successful = True

    def reverse(self):
        # this removes all pyc files even if they were not created
        if self._execute_successful:
            log.trace("reversing pyc creation %s", ' '.join(self.target_full_paths))
            for target_full_path in self.target_full_paths:
                rm_rf(target_full_path)


class AggregateCompileMultiPycAction(CompileMultiPycAction):
    """Bunch up all of our compile actions, so that they all get carried out at once.
    This avoids clobbering and is faster when we have several individual packages requiring
    compilation"""
    def __init__(self, *individuals, **kw):
        transaction_context = individuals[0].transaction_context
        # not used; doesn't matter
        package_info = individuals[0].package_info
        target_prefix = individuals[0].target_prefix
        source_short_paths = set()
        target_short_paths = set()
        for individual in individuals:
            source_short_paths.update(individual.source_short_paths)
            target_short_paths.update(individual.target_short_paths)
        super(AggregateCompileMultiPycAction, self).__init__(
            transaction_context, package_info, target_prefix,
            source_short_paths, target_short_paths)


class CreatePythonEntryPointAction(CreateInPrefixPathAction):

    @classmethod
    def create_actions(cls, transaction_context, package_info, target_prefix, requested_link_type):
        noarch = package_info.package_metadata and package_info.package_metadata.noarch
        if noarch is not None and noarch.type == NoarchType.python:
            def this_triplet(entry_point_def):
                command, module, func = parse_entry_point_def(entry_point_def)
                target_short_path = "%s/%s" % (get_bin_directory_short_path(), command)
                if on_win:
                    target_short_path += "-script.py"
                return target_short_path, module, func

            actions = tuple(cls(transaction_context, package_info, target_prefix,
                                *this_triplet(ep_def))
                            for ep_def in noarch.entry_points or ())

            if on_win:  # pragma: unix no cover
                actions += tuple(
                    LinkPathAction.create_python_entry_point_windows_exe_action(
                        transaction_context, package_info, target_prefix,
                        requested_link_type, ep_def
                    ) for ep_def in noarch.entry_points or ()
                )

            return actions
        else:
            return ()

    def __init__(self, transaction_context, package_info, target_prefix, target_short_path,
                 module, func):
        super(CreatePythonEntryPointAction, self).__init__(transaction_context, package_info,
                                                           None, None,
                                                           target_prefix, target_short_path)
        self.module = module
        self.func = func

        if on_win:
            path_type = PathType.windows_python_entry_point_script
        else:
            path_type = PathType.unix_python_entry_point
        self.prefix_path_data = PathDataV1(
            _path=self.target_short_path,
            path_type=path_type,
        )

        self._execute_successful = False

    def execute(self):
        log.trace("creating python entry point %s", self.target_full_path)
        if on_win:
            python_full_path = None
        else:
            target_python_version = self.transaction_context['target_python_version']
            python_short_path = get_python_short_path(target_python_version)
            python_full_path = join(
                context.target_prefix_override or self.target_prefix,
                win_path_ok(python_short_path))

        create_python_entry_point(self.target_full_path, python_full_path,
                                  self.module, self.func)
        self._execute_successful = True

    def reverse(self):
        if self._execute_successful:
            log.trace("reversing python entry point creation %s", self.target_full_path)
            rm_rf(self.target_full_path)


# class CreateApplicationEntryPointWindowsExeAction(LinkPathAction):
#
#     @classmethod
#     def create_actions(cls, transaction_context, package_info, target_prefix, requested_link_type,  # NOQA
#                        exe_path):
#         source_directory = context.conda_prefix
#         source_short_path = 'Scripts/conda.exe'
#         target_short_path = exe_path
#         return cls(transaction_context, package_info, source_directory,
#                    source_short_path, target_prefix, target_short_path, requested_link_type)
#
#     def __init__(self, transaction_context, package_info, source_prefix, source_short_path,
#                  target_prefix, target_short_path, requested_link_type):
#         super(CreateApplicationEntryPointWindowsExeAction, self).__init__(
#             transaction_context, package_info, source_prefix, source_short_path,
#             target_prefix, target_short_path, requested_link_type,
#         )
#         self.leased_path_entry = LeasedPathEntry(
#             _path=target_short_path,
#             target_path=self.source_full_path,
#             target_prefix=source_prefix,
#             leased_path=self.target_full_path,
#             package_name=package_info.index_json_record.name,
#             leased_path_type=self.leased_path_type,
#         )
#
#     @property
#     def leased_path_type(self):
#         return LeasedPathType.application_entry_point_windows_exe


# class CreateApplicationEntryPointAction(CreateLeasedPathAction):
#
#     @classmethod
#     def create_actions(cls, transaction_context, package_info, target_prefix, requested_link_type):  # NOQA
#         preferred_env = package_info.repodata_record.preferred_env
#         if preferred_env_matches_prefix(preferred_env, target_prefix, context.root_prefix):
#             exe_paths = (package_info.package_metadata
#                          and package_info.package_metadata.preferred_env
#                          and package_info.package_metadata.preferred_env.executable_paths
#                          or ())
#
#             # target_prefix for the instantiated path action is the root prefix, not the same
#             #   as target_prefix for the larger transaction
#             assert is_private_env_path(target_prefix)
#             root_prefix = dirname(dirname(target_prefix))
#
#             if on_win:
#                 def make_app_entry_point_axns(exe_path):
#                     assert exe_path.endswith(('.exe', '.bat'))
#                     target_short_path = exe_path[:-4] + "-script.py"
#                     yield cls(transaction_context, package_info, target_prefix, exe_path,
#                               root_prefix, target_short_path)
#
#                     yield CreateApplicationEntryPointWindowsExeAction.create_actions(
#                         transaction_context, package_info, root_prefix,
#                         LinkType.hardlink, exe_path[:-4] + ".exe"
#                     )
#                 return tuple(concat(make_app_entry_point_axns(executable_short_path)
#                                     for executable_short_path in exe_paths))
#
#             else:
#                 return tuple(
#                     cls(transaction_context, package_info, target_prefix, executable_short_path,
#                         root_prefix, executable_short_path)
#                     for executable_short_path in exe_paths
#                 )
#         else:
#             return ()
#
#     def execute(self):
#         log.trace("creating application entry point %s => %s",
#                   self.source_full_path, self.target_full_path)
#         if self.source_prefix == context.conda_prefix:
#             # this could blow up for the special case of application entry points in conda's
#             #   private environment
#             # in that case, probably should use the python version from transaction_context
#             conda_python_version = self.transaction_context['target_python_version']
#         else:
#             conda_python_version = get_python_version_for_prefix(context.conda_prefix)
#         conda_python_short_path = get_python_short_path(conda_python_version)
#         conda_python_full_path = join(context.conda_prefix, win_path_ok(conda_python_short_path))
#         create_application_entry_point(self.source_full_path, self.target_full_path,
#                                        conda_python_full_path)
#         self._execute_successful = True
#
#     @property
#     def leased_path_type(self):
#         return LeasedPathType.application_entry_point
#
#
# class CreateApplicationSoftlinkAction(CreateLeasedPathAction):
#
#     @classmethod
#     def create_actions(cls, transaction_context, package_info, target_prefix, requested_link_type):  # NOQA
#         preferred_env = package_info.repodata_record.preferred_env
#         if preferred_env_matches_prefix(preferred_env, target_prefix, context.root_prefix):
#             softlink_paths = (package_info.package_metadata
#                               and package_info.package_metadata.preferred_env
#                               and package_info.package_metadata.preferred_env.softlink_paths
#                               or ())
#
#             # target_prefix for the instantiated path action is the root prefix, not the same
#             #   as target_prefix for the larger transaction
#             assert is_private_env_path(target_prefix)
#             root_prefix = dirname(dirname(target_prefix))
#             softlink_supported_test_file = join(target_prefix, PREFIX_MAGIC_FILE)
#
#             def make_softlink_exe_axn(softlink_short_path):
#                 if not on_win:  # pragma: win no cover
#                     root_short_path = softlink_short_path
#                     softlink_method = 'softlink'
#                 else:  # pragma: unix no cover
#                     windows_pathext = os.getenv('PATHEXT', '').lower().split(';')
#                     path_root, path_ext = splitext(softlink_short_path)
#
#                     if softlink_supported(softlink_supported_test_file, root_prefix):
#                         root_short_path = softlink_short_path
#                         softlink_method = 'softlink'
#                     elif path_ext.lower() in windows_pathext:
#                         root_short_path = splitext(softlink_short_path)[0] + '.bat'
#                         softlink_method = 'fake_exe_softlink'
#                     else:
#                         root_short_path = softlink_short_path
#                         softlink_method = 'softlink_or_fail_ok'
#
#                 return cls(transaction_context, package_info, target_prefix, softlink_short_path,
#                            root_prefix, root_short_path, softlink_method)
#
#             return tuple(make_softlink_exe_axn(softlink_short_path)
#                          for softlink_short_path in softlink_paths)
#
#         else:
#             return ()
#
#     def __init__(self, transaction_context, package_info, source_prefix, source_short_path,
#                  target_prefix, target_short_path, softlink_method):
#         super(CreateApplicationSoftlinkAction, self).__init__(transaction_context, package_info,
#                                                               source_prefix, source_short_path,
#                                                               target_prefix, target_short_path)
#         self.softlink_method = softlink_method
#
#     def execute(self):
#         log.trace("creating application softlink via %s %s => %s",
#                   self.softlink_method, self.source_full_path, self.target_full_path)
#         getattr(self, self.softlink_method)()
#         self._execute_successful = True
#
#     def softlink(self):
#         symlink(self.source_full_path, self.target_full_path)
#         assert islink(self.target_full_path)
#
#     def fake_exe_softlink(self):  # pragma: unix no cover
#         create_fake_executable_softlink(self.source_full_path, self.target_full_path)
#
#     def softlink_or_fail_ok(self):  # pragma: unix no cover
#         try:
#             symlink(self.source_full_path, self.target_full_path)
#         except (IOError, OSError) as e:
#             log.trace('%r', e)
#
#     @property
#     def leased_path_type(self):
#         return LeasedPathType.application_softlink


class CreatePrefixRecordAction(CreateInPrefixPathAction):
    # this is the action that creates a packages json file in the conda-meta/ directory

    @classmethod
    def create_actions(cls, transaction_context, package_info, target_prefix, requested_link_type,
                       requested_spec, all_link_path_actions):

        extracted_package_dir = package_info.extracted_package_dir
        target_short_path = 'conda-meta/%s.json' % basename(extracted_package_dir)
        return cls(transaction_context, package_info, target_prefix, target_short_path,
                   requested_link_type, requested_spec, all_link_path_actions),

    def __init__(self, transaction_context, package_info, target_prefix, target_short_path,
                 requested_link_type, requested_spec, all_link_path_actions):
        super(CreatePrefixRecordAction, self).__init__(transaction_context, package_info,
                                                       None, None, target_prefix,
                                                       target_short_path)
        self.requested_link_type = requested_link_type
        self.requested_spec = requested_spec
        self.all_link_path_actions = list(all_link_path_actions)
        self._execute_successful = False

    def execute(self):
        link = Link(
            source=self.package_info.extracted_package_dir,
            type=self.requested_link_type,
        )
        extracted_package_dir = self.package_info.extracted_package_dir
        package_tarball_full_path = self.package_info.package_tarball_full_path

        def files_from_action(link_path_action):
            if isinstance(link_path_action, CompileMultiPycAction):
                return link_path_action.target_short_paths
            else:
                return ((link_path_action.target_short_path, )
                        if isinstance(link_path_action, CreateInPrefixPathAction) and
                        (not hasattr(link_path_action, 'link_type') or
                         link_path_action.link_type != LinkType.directory) else ())

        def paths_from_action(link_path_action):
            if isinstance(link_path_action, CompileMultiPycAction):
                return link_path_action.prefix_paths_data
            else:
                if (not hasattr(link_path_action, 'prefix_path_data') or
                        link_path_action.prefix_path_data is None):
                    return ()
                else:
                    return (link_path_action.prefix_path_data, )

        files = list(concat(files_from_action(x) for x in self.all_link_path_actions if x))
        paths_data = PathsData(
            paths_version=1,
            paths=concat((paths_from_action(x) for x in self.all_link_path_actions if x)),
        )

        self.prefix_record = PrefixRecord.from_objects(
            self.package_info.repodata_record,
            # self.package_info.index_json_record,
            self.package_info.package_metadata,
            requested_spec=str(self.requested_spec),
            paths_data=paths_data,
            files=files,
            link=link,
            url=self.package_info.url,
            extracted_package_dir=extracted_package_dir,
            package_tarball_full_path=package_tarball_full_path,
        )

        log.trace("creating linked package record %s", self.target_full_path)
        PrefixData(self.target_prefix).insert(self.prefix_record)
        self._execute_successful = True

    def reverse(self):
        log.trace("reversing linked package record creation %s", self.target_full_path)
        if self._execute_successful:
            PrefixData(self.target_prefix).remove(self.package_info.repodata_record.name)


class UpdateHistoryAction(CreateInPrefixPathAction):

    @classmethod
    def create_actions(cls, transaction_context, target_prefix, remove_specs, update_specs,
                       neutered_specs):
        target_short_path = join('conda-meta', 'history')
        return cls(transaction_context, target_prefix, target_short_path,
                   remove_specs, update_specs, neutered_specs),

    def __init__(self, transaction_context, target_prefix, target_short_path, remove_specs,
                 update_specs, neutered_specs):
        super(UpdateHistoryAction, self).__init__(transaction_context, None, None, None,
                                                  target_prefix, target_short_path)
        self.remove_specs = remove_specs
        self.update_specs = update_specs
        self.neutered_specs = neutered_specs

        self.hold_path = self.target_full_path + CONDA_TEMP_EXTENSION

    def execute(self):
        log.trace("updating environment history %s", self.target_full_path)

        if lexists(self.target_full_path):
            copy(self.target_full_path, self.hold_path)

        h = History(self.target_prefix)
        h.update()
        h.write_specs(self.remove_specs, self.update_specs, self.neutered_specs)

    def reverse(self):
        if lexists(self.hold_path):
            log.trace("moving %s => %s", self.hold_path, self.target_full_path)
            backoff_rename(self.hold_path, self.target_full_path, force=True)

    def cleanup(self):
        rm_rf(self.hold_path)


class RegisterEnvironmentLocationAction(PathAction):

    def __init__(self, transaction_context, target_prefix):
        self.transaction_context = transaction_context
        self.target_prefix = target_prefix

        self._execute_successful = False

    def verify(self):
        user_environments_txt_file = get_user_environments_txt_file()
        try:
            touch(user_environments_txt_file, mkdir=True, sudo_safe=True)
            self._verified = True
        except NotWritableError:
            log.warn("Unable to create environments file. Path not writable.\n"
                     "  environment location: %s\n", user_environments_txt_file)

    def execute(self):
        log.trace("registering environment in catalog %s", self.target_prefix)

        register_env(self.target_prefix)
        self._execute_successful = True

    def reverse(self):
        pass

    def cleanup(self):
        pass

    @property
    def target_full_path(self):
        raise NotImplementedError()


# ######################################################
#  Removal of Paths within a Prefix
# ######################################################

class RemoveFromPrefixPathAction(PrefixPathAction, metaclass=ABCMeta):

    def __init__(self, transaction_context, linked_package_data, target_prefix, target_short_path):
        super(RemoveFromPrefixPathAction, self).__init__(transaction_context,
                                                         target_prefix, target_short_path)
        self.linked_package_data = linked_package_data

    def verify(self):
        # inability to remove will trigger a rollback
        # can't definitely know if path can be removed until it's attempted and failed
        self._verified = True


class UnlinkPathAction(RemoveFromPrefixPathAction):
    def __init__(self, transaction_context, linked_package_data, target_prefix, target_short_path,
                 link_type=LinkType.hardlink):
        super(UnlinkPathAction, self).__init__(transaction_context, linked_package_data,
                                               target_prefix, target_short_path)
        self.holding_short_path = self.target_short_path + CONDA_TEMP_EXTENSION
        self.holding_full_path = self.target_full_path + CONDA_TEMP_EXTENSION
        self.link_type = link_type

    def execute(self):
        if self.link_type != LinkType.directory:
            log.trace("renaming %s => %s", self.target_short_path, self.holding_short_path)
            backoff_rename(self.target_full_path, self.holding_full_path, force=True)

    def reverse(self):
        if self.link_type != LinkType.directory and lexists(self.holding_full_path):
            log.trace("reversing rename %s => %s", self.holding_short_path, self.target_short_path)
            backoff_rename(self.holding_full_path, self.target_full_path, force=True)

    def cleanup(self):
        if not isdir(self.holding_full_path):
            rm_rf(self.holding_full_path, clean_empty_parents=True)


class RemoveMenuAction(RemoveFromPrefixPathAction):

    @classmethod
    def create_actions(cls, transaction_context, linked_package_data, target_prefix):
        if on_win:
            MENU_RE = re.compile(r'^menu/.*\.json$', re.IGNORECASE)
            return tuple(cls(transaction_context, linked_package_data, target_prefix, trgt)
                         for trgt in linked_package_data.files if bool(MENU_RE.match(trgt)))
        else:
            return ()

    def __init__(self, transaction_context, linked_package_data,
                 target_prefix, target_short_path):
        super(RemoveMenuAction, self).__init__(transaction_context, linked_package_data,
                                               target_prefix, target_short_path)

    def execute(self):
        log.trace("removing menu for %s ", self.target_prefix)
        make_menu(self.target_prefix, self.target_short_path, remove=True)

    def reverse(self):
        log.trace("re-creating menu for %s ", self.target_prefix)
        make_menu(self.target_prefix, self.target_short_path, remove=False)

    def cleanup(self):
        pass


class RemoveLinkedPackageRecordAction(UnlinkPathAction):

    def __init__(self, transaction_context, linked_package_data, target_prefix, target_short_path):
        super(RemoveLinkedPackageRecordAction, self).__init__(transaction_context,
                                                              linked_package_data,
                                                              target_prefix, target_short_path)

    def execute(self):
        super(RemoveLinkedPackageRecordAction, self).execute()
        PrefixData(self.target_prefix).remove(self.linked_package_data.name)

    def reverse(self):
        super(RemoveLinkedPackageRecordAction, self).reverse()
        PrefixData(self.target_prefix)._load_single_record(self.target_full_path)


class UnregisterEnvironmentLocationAction(PathAction):

    def __init__(self, transaction_context, target_prefix):
        self.transaction_context = transaction_context
        self.target_prefix = target_prefix

        self._execute_successful = False

    def verify(self):
        self._verified = True

    def execute(self):
        log.trace("unregistering environment in catalog %s", self.target_prefix)

        unregister_env(self.target_prefix)
        self._execute_successful = True

    def reverse(self):
        pass

    def cleanup(self):
        pass

    @property
    def target_full_path(self):
        raise NotImplementedError()


# ######################################################
#  Fetch / Extract Actions
# ######################################################

class CacheUrlAction(PathAction):

    def __init__(self, url, target_pkgs_dir, target_package_basename,
                 sha256=None, size=None, md5=None):
        self.url = url
        self.target_pkgs_dir = target_pkgs_dir
        self.target_package_basename = target_package_basename
        self.sha256 = sha256
        self.size = size
        self.md5 = md5
        self.hold_path = self.target_full_path + CONDA_TEMP_EXTENSION

    def verify(self):
        assert '::' not in self.url
        self._verified = True

    def execute(self, progress_update_callback=None):
        # I hate inline imports, but I guess it's ok since we're importing from the conda.core
        # The alternative is passing the PackageCache class to CacheUrlAction __init__
        from .package_cache_data import PackageCacheData
        target_package_cache = PackageCacheData(self.target_pkgs_dir)

        log.trace("caching url %s => %s", self.url, self.target_full_path)

        if lexists(self.hold_path):
            rm_rf(self.hold_path)

        if lexists(self.target_full_path):
            if self.url.startswith('file:/') and self.url == path_to_url(self.target_full_path):
                # the source and destination are the same file, so we're done
                return
            else:
                backoff_rename(self.target_full_path, self.hold_path, force=True)

        if self.url.startswith('file:/'):
            source_path = url_to_path(self.url)
            self._execute_local(source_path, target_package_cache, progress_update_callback)
        else:
            self._execute_channel(target_package_cache, progress_update_callback)

    def _execute_local(self, source_path, target_package_cache, progress_update_callback=None):
        from .package_cache_data import PackageCacheData
        if dirname(source_path) in context.pkgs_dirs:
            # if url points to another package cache, link to the writable cache
            create_hard_link_or_copy(source_path, self.target_full_path)
            source_package_cache = PackageCacheData(dirname(source_path))

            # the package is already in a cache, so it came from a remote url somewhere;
            #   make sure that remote url is the most recent url in the
            #   writable cache urls.txt
            origin_url = source_package_cache._urls_data.get_url(self.target_package_basename)
            if origin_url and has_platform(origin_url, context.known_subdirs):
                target_package_cache._urls_data.add_url(origin_url)
        else:
            # so our tarball source isn't a package cache, but that doesn't mean it's not
            #   in another package cache somewhere
            # let's try to find the actual, remote source url by matching md5sums, and then
            #   record that url as the remote source url in urls.txt
            # we do the search part of this operation before the create_link so that we
            #   don't md5sum-match the file created by 'create_link'
            # there is no point in looking for the tarball in the cache that we are writing
            #   this file into because we have already removed the previous file if there was
            #   any. This also makes sure that we ignore the md5sum of a possible extracted
            #   directory that might exist in this cache because we are going to overwrite it
            #   anyway when we extract the tarball.
            source_md5sum = compute_md5sum(source_path)
            exclude_caches = self.target_pkgs_dir,
            pc_entry = PackageCacheData.tarball_file_in_cache(
                source_path, source_md5sum, exclude_caches=exclude_caches
            )

            if pc_entry:
                origin_url = target_package_cache._urls_data.get_url(
                    pc_entry.extracted_package_dir
                )
            else:
                origin_url = None

            # copy the tarball to the writable cache
            create_link(source_path, self.target_full_path, link_type=LinkType.copy,
                        force=context.force)

            if origin_url and has_platform(origin_url, context.known_subdirs):
                target_package_cache._urls_data.add_url(origin_url)
            else:
                target_package_cache._urls_data.add_url(self.url)

    def _execute_channel(self, target_package_cache, progress_update_callback=None):
        kwargs = {}
        if self.size is not None:
            kwargs["size"] = self.size
        if self.sha256:
            kwargs["sha256"] = self.sha256
        elif self.md5:
            kwargs["md5"] = self.md5
        download(
            self.url,
            self.target_full_path,
            progress_update_callback=progress_update_callback,
            **kwargs
        )
        target_package_cache._urls_data.add_url(self.url)

    def reverse(self):
        if lexists(self.hold_path):
            log.trace("moving %s => %s", self.hold_path, self.target_full_path)
            backoff_rename(self.hold_path, self.target_full_path, force=True)

    def cleanup(self):
        rm_rf(self.hold_path)

    @property
    def target_full_path(self):
        return join(self.target_pkgs_dir, self.target_package_basename)

    def __str__(self):
        return 'CacheUrlAction<url=%r, target_full_path=%r>' % (self.url, self.target_full_path)


class ExtractPackageAction(PathAction):

    def __init__(self, source_full_path, target_pkgs_dir, target_extracted_dirname,
                 record_or_spec, sha256, size, md5):
        self.source_full_path = source_full_path
        self.target_pkgs_dir = target_pkgs_dir
        self.target_extracted_dirname = target_extracted_dirname
        self.hold_path = self.target_full_path + CONDA_TEMP_EXTENSION
        self.record_or_spec = record_or_spec
        self.sha256 = sha256
        self.size = size
        self.md5 = md5

    def verify(self):
        self._verified = True

    def execute(self, progress_update_callback=None):
        # I hate inline imports, but I guess it's ok since we're importing from the conda.core
        # The alternative is passing the the classes to ExtractPackageAction __init__
        from .package_cache_data import PackageCacheData
        log.trace("extracting %s => %s", self.source_full_path, self.target_full_path)

        if lexists(self.target_full_path):
            rm_rf(self.target_full_path)

        extract_tarball(self.source_full_path, self.target_full_path,
                        progress_update_callback=progress_update_callback)

        try:
            raw_index_json = read_index_json(self.target_full_path)
        except (IOError, OSError, JSONDecodeError, FileNotFoundError):
            # At this point, we can assume the package tarball is bad.
            # Remove everything and move on.
            print("ERROR: Encountered corrupt package tarball at %s. Conda has "
                  "left it in place. Please report this to the maintainers "
                  "of the package." % self.source_full_path)
            sys.exit(1)

        if isinstance(self.record_or_spec, MatchSpec):
            url = self.record_or_spec.get_raw_value('url')
            assert url
            channel = Channel(url) if has_platform(url, context.known_subdirs) else Channel(None)
            fn = basename(url)
            sha256 = self.sha256 or compute_sha256sum(self.source_full_path)
            size = getsize(self.source_full_path)
            if self.size is not None:
                assert size == self.size, (size, self.size)
            md5 = self.md5 or compute_md5sum(self.source_full_path)
            repodata_record = PackageRecord.from_objects(
                raw_index_json, url=url, channel=channel, fn=fn, sha256=sha256, size=size, md5=md5,
            )
        else:
            repodata_record = PackageRecord.from_objects(self.record_or_spec, raw_index_json)

        repodata_record_path = join(self.target_full_path, 'info', 'repodata_record.json')
        write_as_json_to_file(repodata_record_path, repodata_record)

        target_package_cache = PackageCacheData(self.target_pkgs_dir)
        package_cache_record = PackageCacheRecord.from_objects(
            repodata_record,
            package_tarball_full_path=self.source_full_path,
            extracted_package_dir=self.target_full_path,
        )
        target_package_cache.insert(package_cache_record)

    def reverse(self):
        rm_rf(self.target_full_path)
        if lexists(self.hold_path):
            log.trace("moving %s => %s", self.hold_path, self.target_full_path)
            rm_rf(self.target_full_path)
            backoff_rename(self.hold_path, self.target_full_path)

    def cleanup(self):
        rm_rf(self.hold_path)

    @property
    def target_full_path(self):
        return join(self.target_pkgs_dir, self.target_extracted_dirname)

    def __str__(self):
        return ('ExtractPackageAction<source_full_path=%r, target_full_path=%r>'
                % (self.source_full_path, self.target_full_path))
