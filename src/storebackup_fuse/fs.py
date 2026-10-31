# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Thomas Wagner
"""Read-only FUSE filesystem showing one storeBackup backup as plain files."""

import errno
import itertools
import logging
import os

import pyfuse3
import trio

from .backup import DIR, FILE, SYMLINK, BackupError, ChecksumError

log = logging.getLogger(__name__)


class StoreBackupFS(pyfuse3.Operations):

    def __init__(self, backup, verify=True):
        super().__init__()
        self.backup = backup
        self.verify = verify
        self.handles = {}
        self.sorted_names = {}
        self.next_fh = itertools.count(1)

    # -- metadata ----------------------------------------------------------

    def _entry(self, inode):
        try:
            return self.backup.by_inode[inode]
        except KeyError:
            raise pyfuse3.FUSEError(errno.ENOENT) from None

    def _attr(self, entry):
        attr = pyfuse3.EntryAttributes()
        attr.st_ino = entry.inode
        attr.st_mode = entry.st_mode
        attr.st_nlink = entry.nlink
        attr.st_uid = entry.uid
        attr.st_gid = entry.gid
        attr.st_rdev = self.backup.rdev(entry)
        if entry.kind == FILE:
            attr.st_size = entry.size
        elif entry.kind == SYMLINK:
            attr.st_size = len(self.backup.readlink(entry))
        else:
            attr.st_size = 0
        attr.st_blksize = 1 << 17
        attr.st_blocks = (attr.st_size + 511) // 512
        attr.st_atime_ns = entry.atime * 10**9
        attr.st_mtime_ns = entry.mtime * 10**9
        attr.st_ctime_ns = entry.ctime * 10**9
        # nothing ever changes: let the kernel cache everything
        attr.attr_timeout = 3600
        attr.entry_timeout = 3600
        return attr

    async def getattr(self, inode, ctx=None):
        return self._attr(self._entry(inode))

    async def lookup(self, parent_inode, name, ctx=None):
        parent = self._entry(parent_inode)
        if parent.kind != DIR:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        if name == b'.':
            child = parent
        elif name == b'..':
            child = parent.parent or parent
        else:
            child = parent.children.get(name)
        if child is None:
            raise pyfuse3.FUSEError(errno.ENOENT)
        return self._attr(child)

    async def readlink(self, inode, ctx):
        entry = self._entry(inode)
        if entry.kind != SYMLINK:
            raise pyfuse3.FUSEError(errno.EINVAL)
        try:
            return self.backup.readlink(entry)
        except OSError as e:
            log.error('readlink %r: %s', entry.path, e)
            raise pyfuse3.FUSEError(errno.EIO) from None

    async def opendir(self, inode, ctx):
        entry = self._entry(inode)
        if entry.kind != DIR:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        return inode

    async def readdir(self, fh, start_id, token):
        entry = self._entry(fh)
        # large directories are read in many calls: sort only once
        names = self.sorted_names.get(fh)
        if names is None:
            names = self.sorted_names[fh] = sorted(entry.children)
        for i in range(start_id, len(names)):
            child = entry.children[names[i]]
            if not pyfuse3.readdir_reply(token, names[i], self._attr(child),
                                         i + 1):
                break

    async def releasedir(self, fh):
        self.sorted_names.pop(fh, None)

    async def statfs(self, ctx):
        st = pyfuse3.StatvfsData()
        st.f_bsize = st.f_frsize = 4096
        st.f_files = len(self.backup.by_inode)
        st.f_namemax = 255
        return st

    async def getxattr(self, inode, name, ctx):
        raise pyfuse3.FUSEError(errno.ENODATA)

    async def listxattr(self, inode, ctx):
        return []

    # -- contents ----------------------------------------------------------

    async def open(self, inode, flags, ctx):
        entry = self._entry(inode)
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_TRUNC | os.O_APPEND):
            raise pyfuse3.FUSEError(errno.EROFS)
        if entry.kind != FILE:
            raise pyfuse3.FUSEError(errno.EINVAL)
        try:
            reader = await trio.to_thread.run_sync(
                self.backup.open, entry, self.verify)
        except (OSError, BackupError) as e:
            log.error('open %r: %s', entry.path, e)
            raise pyfuse3.FUSEError(errno.EIO) from None
        fh = next(self.next_fh)
        self.handles[fh] = reader
        return pyfuse3.FileInfo(fh=fh, keep_cache=True)

    async def read(self, fh, off, size):
        reader = self.handles[fh]
        try:
            # answering in the event loop avoids a thread switch per
            # request, which costs ~40% throughput; only done when the data
            # is at hand, so slow reads do not block other requests
            if reader.ready(off, size):
                return reader.read(off, size)
            return await trio.to_thread.run_sync(reader.read, off, size)
        except ChecksumError as e:
            log.error('%s', e)
            raise pyfuse3.FUSEError(errno.EIO) from None
        except (OSError, BackupError, EOFError) as e:
            log.error('read %r at %d: %s', reader.entry.path
                      if hasattr(reader, 'entry') else fh, off, e)
            raise pyfuse3.FUSEError(errno.EIO) from None

    async def release(self, fh):
        reader = self.handles.pop(fh, None)
        if reader is not None:
            await trio.to_thread.run_sync(reader.close)


def mount(backup, mountpoint, verify=True, allow_other=False, debug=False):
    """Mount ``backup`` at ``mountpoint`` and serve until unmounted."""
    fs = StoreBackupFS(backup, verify=verify)
    options = set(pyfuse3.default_options)
    options.discard('default_permissions')
    options |= {'fsname=storebackup', 'subtype=storebackup', 'ro',
                'default_permissions'}
    if allow_other:
        options.add('allow_other')
    if debug:
        options.add('debug')
    pyfuse3.init(fs, mountpoint, options)
    try:
        trio.run(pyfuse3.main)
    except BaseException:
        pyfuse3.close(unmount=True)
        raise
    pyfuse3.close(unmount=True)
