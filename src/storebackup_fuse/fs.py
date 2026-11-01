# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Thomas Wagner
"""Read-only FUSE filesystem showing one storeBackup backup as plain files."""

import errno
import functools
import itertools
import logging
import os
import signal
import threading

import pyfuse3
import trio

from .backup import (BLOCKDEV, CHARDEV, DECOMPRESSION_ERRORS, DIR, FILE,
                     SYMLINK, BackupError, ChecksumError)

log = logging.getLogger(__name__)


def _handler(func):
    """Turn unexpected exceptions into EIO: pyfuse3 ends the whole mount on
    any exception other than FUSEError."""
    @functools.wraps(func)
    async def wrapper(self, *args):
        try:
            return await func(self, *args)
        except pyfuse3.FUSEError:
            raise
        except Exception:
            log.exception('%s%r failed', func.__name__, args)
            raise pyfuse3.FUSEError(errno.EIO) from None
    return wrapper


class StoreBackupFS(pyfuse3.Operations):

    def __init__(self, backup, verify=True):
        super().__init__()
        self.backup = backup
        self.verify = verify
        self.handles = {}
        self.dir_handles = {}       # fh -> [inode, sorted names or None]
        self.next_fh = itertools.count(1)
        # files not verified yet with an open handle that uses the page cache
        self.cached_inodes = set()
        self.link_sizes = {}
        self.rdevs = {}

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
        # rdev and symlink sizes need the disk: once per entry only
        if entry.kind in (BLOCKDEV, CHARDEV):
            if entry.inode not in self.rdevs:
                self.rdevs[entry.inode] = self.backup.rdev(entry)
            attr.st_rdev = self.rdevs[entry.inode]
        if entry.kind == FILE:
            attr.st_size = entry.size
        elif entry.kind == SYMLINK:
            if entry.inode not in self.link_sizes:
                try:
                    self.link_sizes[entry.inode] = len(
                        self.backup.readlink(entry))
                except OSError:
                    self.link_sizes[entry.inode] = 0    # readlink() logs
            attr.st_size = self.link_sizes[entry.inode]
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

    @_handler
    async def getattr(self, inode, ctx=None):
        return self._attr(self._entry(inode))

    @_handler
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

    @_handler
    async def readlink(self, inode, ctx):
        entry = self._entry(inode)
        if entry.kind != SYMLINK:
            raise pyfuse3.FUSEError(errno.EINVAL)
        try:
            return self.backup.readlink(entry)
        except OSError as e:
            log.error('readlink %r: %s', entry.path, e)
            raise pyfuse3.FUSEError(errno.EIO) from None

    @_handler
    async def opendir(self, inode, ctx):
        entry = self._entry(inode)
        if entry.kind != DIR:
            raise pyfuse3.FUSEError(errno.ENOTDIR)
        fh = next(self.next_fh)
        self.dir_handles[fh] = [inode, None]
        return fh

    @_handler
    async def readdir(self, fh, start_id, token):
        handle = self.dir_handles[fh]
        entry = self._entry(handle[0])
        # large directories are read in many calls: sort only once
        names = handle[1]
        if names is None:
            names = handle[1] = sorted(entry.children)
        for i in range(start_id, len(names)):
            child = entry.children[names[i]]
            if not pyfuse3.readdir_reply(token, names[i], self._attr(child),
                                         i + 1):
                break

    @_handler
    async def releasedir(self, fh):
        self.dir_handles.pop(fh, None)

    @_handler
    async def statfs(self, ctx):
        st = pyfuse3.StatvfsData()
        st.f_bsize = st.f_frsize = 4096
        st.f_files = len(self.backup.by_inode)
        st.f_namemax = 255
        return st

    @_handler
    async def getxattr(self, inode, name, ctx):
        raise pyfuse3.FUSEError(errno.ENODATA)

    @_handler
    async def listxattr(self, inode, ctx):
        return []

    # -- contents ----------------------------------------------------------

    @_handler
    async def open(self, inode, flags, ctx):
        entry = self._entry(inode)
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_TRUNC | os.O_APPEND):
            raise pyfuse3.FUSEError(errno.EROFS)
        if entry.kind != FILE:
            raise pyfuse3.FUSEError(errno.EINVAL)
        try:
            reader = await trio.to_thread.run_sync(
                self.backup.open, entry, self.verify)
        except (OSError, BackupError) + DECOMPRESSION_ERRORS as e:
            log.error('open %r: %s', entry.path, e)
            raise pyfuse3.FUSEError(errno.EIO) from None
        fh = next(self.next_fh)
        if not self.verify or entry.inode in self.backup.verified:
            self.handles[fh] = (entry, reader, False)
            return pyfuse3.FileInfo(fh=fh, keep_cache=True)
        # Not checked yet: every reader checks what it reads, so it must see
        # all reads of the file itself.  One reader may use the page cache
        # (dropping pages left by earlier incomplete reads); others read
        # directly while it is open.
        if entry.inode in self.cached_inodes:
            self.handles[fh] = (entry, reader, False)
            return pyfuse3.FileInfo(fh=fh, direct_io=True)
        self.cached_inodes.add(entry.inode)
        self.handles[fh] = (entry, reader, True)
        return pyfuse3.FileInfo(fh=fh, keep_cache=False)

    @_handler
    async def read(self, fh, off, size):
        entry, reader, _ = self.handles[fh]
        try:
            # answering in the event loop avoids a thread switch per
            # request, which costs ~40% throughput; only done when the data
            # is at hand, so slow reads do not block other requests
            data = reader.read_nowait(off, size)
            if data is None:
                data = await trio.to_thread.run_sync(reader.read, off, size)
            return data
        except ChecksumError as e:
            log.error('read %r at %d: %s', entry.path, off, e)
            self._invalidate(entry.inode)
            raise pyfuse3.FUSEError(errno.EIO) from None
        except (OSError, BackupError) + DECOMPRESSION_ERRORS as e:
            log.error('read %r at %d: %s', entry.path, off, e)
            raise pyfuse3.FUSEError(errno.EIO) from None

    @staticmethod
    def _invalidate(inode):
        """Drop the cached pages of a damaged file.  Not from the request
        handler: the kernel keeps the pages locked until the read is
        answered."""
        def run():
            try:
                pyfuse3.invalidate_inode(inode)
            except OSError:
                pass
        threading.Thread(target=run, daemon=True).start()

    @_handler
    async def release(self, fh):
        handle = self.handles.pop(fh, None)
        if handle is not None:
            entry, reader, cached = handle
            if cached:
                self.cached_inodes.discard(entry.inode)
            await trio.to_thread.run_sync(reader.close)


STOP_SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)


def mount(backup, mountpoint, verify=True, allow_other=False, debug=False):
    """Mount ``backup`` at ``mountpoint`` and serve until unmounted or
    stopped by SIGTERM, SIGINT or SIGHUP."""
    fs = StoreBackupFS(backup, verify=verify)
    options = set(pyfuse3.default_options)
    options.discard('default_permissions')
    options |= {'fsname=storebackup', 'subtype=storebackup', 'ro',
                'default_permissions'}
    if allow_other:
        options.add('allow_other')
    if debug:
        options.add('debug')
    # held back until _stop_on_signal is ready, so that a signal cannot
    # leave a mount without its process behind
    signal.pthread_sigmask(signal.SIG_BLOCK, STOP_SIGNALS)
    try:
        pyfuse3.init(fs, mountpoint, options)
        try:
            trio.run(_serve)
        finally:
            pyfuse3.close(unmount=True)
    finally:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)


async def _serve():
    async with trio.open_nursery() as nursery:
        nursery.start_soon(_stop_on_signal, nursery.cancel_scope)
        await pyfuse3.main()
        nursery.cancel_scope.cancel()


async def _stop_on_signal(cancel_scope):
    with trio.open_signal_receiver(*STOP_SIGNALS) as signals:
        signal.pthread_sigmask(signal.SIG_UNBLOCK, STOP_SIGNALS)
        async for signum in signals:
            log.info('%s received, unmounting', signal.Signals(signum).name)
            cancel_scope.cancel()
            return
