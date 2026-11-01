# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Thomas Wagner
"""Read-only model of one storeBackup backup directory.

A storeBackup backup (e.g. ``<backupDir>/<series>/2026.10.31_04.09.14``) is
described by ``.md5CheckSums.info`` and ``.md5CheckSums[.bz2]``.  Each line of
the checksum file has twelve fields::

    md5 compr dev-inode inodeBackup ctime mtime atime size uid gid mode filename

``md5`` is the MD5 of the file contents, or ``dir``, ``symlink``, ``pipe``,
``socket``, ``blockdev``, ``chardev`` for other file types.  ``compr`` is
``u`` (stored as is), ``c`` (stored compressed with the suffix ``postfix``
from the info file) or ``b`` (split into blocks, see ``.md5BlockCheckSums``).
Owner, mode and times of the original file are only in the checksum file:
the stored files are hard links shared between backups, so their own
metadata belongs to whichever backup created them.

This module does not depend on FUSE; :mod:`storebackup_fuse.fs` puts a
filesystem on top of it.
"""

import bz2
import concurrent.futures
import gzip
import hashlib
import logging
import lzma
import os
import re
import shlex
import stat
import subprocess
import threading
import zlib

log = logging.getLogger(__name__)

ROOT_INODE = 1

DIR = 'dir'
FILE = 'file'
SYMLINK = 'symlink'
FIFO = 'pipe'
SOCKET = 'socket'
BLOCKDEV = 'blockdev'
CHARDEV = 'chardev'

_TYPE_BITS = {
    DIR: stat.S_IFDIR,
    FILE: stat.S_IFREG,
    SYMLINK: stat.S_IFLNK,
    FIFO: stat.S_IFIFO,
    SOCKET: stat.S_IFSOCK,
    BLOCKDEV: stat.S_IFBLK,
    CHARDEV: stat.S_IFCHR,
}

CHECKSUM_FILE = '.md5CheckSums'
BLOCK_CHECKSUM_FILE = '.md5BlockCheckSums.bz2'
# a backup is complete when this exists; versions up to 3.4.3 instead write
# NOT_FINISHED_FLAG while running (storeBackupLib.pl:checkIfBackupWasFinished)
FINISHED_FLAG = '.storeBackupLinks/backup.Finished'
NOT_FINISHED_FLAG = '.md5CheckSums.notFinished'
# present until storeBackupUpdateBackup.pl has resolved the links of a
# backup made with lateLinks
UNRESOLVED_LINKS = '.storeBackupLinks/linkFile.bz2'


# errors of the decompression libraries that are no OSError
DECOMPRESSION_ERRORS = (EOFError, lzma.LZMAError, zlib.error)


class BackupError(Exception):
    """The backup cannot be read."""


class ChecksumError(BackupError):
    """Contents of a stored file differ from the checksum file."""


class Entry:
    """One file, directory, symlink or special file of a backup."""

    __slots__ = ('kind', 'compr', 'md5', 'dev_inode', 'ctime', 'mtime',
                 'atime', 'size', 'uid', 'gid', 'mode', 'name', 'path',
                 'inode', 'nlink', 'parent', 'children')

    def __init__(self, kind, name, path):
        self.kind = kind
        self.name = name            # bytes, last path component
        self.path = path            # bytes, relative to the backup root
        self.compr = None
        self.md5 = None
        self.dev_inode = None
        self.ctime = self.mtime = self.atime = 0
        self.size = 0
        self.uid = self.gid = 0
        self.mode = 0o755 if kind == DIR else 0
        self.inode = None
        self.nlink = 1
        self.parent = None
        self.children = {} if kind == DIR else None

    @property
    def st_mode(self):
        if self.kind == SYMLINK:
            # storeBackup records 0; symlinks on Linux are always rwxrwxrwx
            return stat.S_IFLNK | 0o777
        return _TYPE_BITS[self.kind] | (self.mode & 0o7777)

    def __repr__(self):
        return f'<Entry {self.kind} {self.path!r}>'


def unescape_name(name):
    """Undo storeBackup's escaping of newline and backslash in file names
    (same order as readCheckSumFile::evalBinLine)."""
    return name.replace(b'\\0A', b'\n').replace(b'\\5C', b'\\')


def parse_info(path):
    """Parse ``.md5CheckSums.info`` into a dict of key -> list of words.

    Values are written by storeBackup as shell-like quoted words, e.g.
    ``uncompress='bzip2' '-d'``.
    """
    info = {}
    with open(path, 'rb') as f:
        for raw in f:
            line = raw.decode('utf-8', 'surrogateescape').rstrip('\n')
            if '=' not in line or line.startswith('#'):
                continue
            key, value = line.split('=', 1)
            try:
                info[key] = shlex.split(value)
            except ValueError:
                info[key] = [value]
    return info


def parse_version(text):
    """storeBackup version as tuple, e.g. (3, 5, 2); () if there is none."""
    m = re.match(r'(\d+(?:\.\d+)*)', text)
    return tuple(int(n) for n in m.group(1).split('.')) if m else ()


def check_relative_path(path, what):
    """Refuse paths that could point outside the backup directory."""
    if path.startswith(b'/') or any(
            part in (b'', b'.', b'..') for part in path.split(b'/')):
        raise BackupError(f'{what} {path!r} is not a plain relative path')


def parse_checksum_line(line):
    """Split one line of the checksum file into its twelve fields.

    Returns None for lines that are not entries (comment, malformed).
    storeBackup separates the fields by exactly one space, so a name may
    start with white space.
    """
    fields = line.rstrip(b'\n').split(b' ', 11)
    if len(fields) != 12 or fields[0].startswith(b'#'):
        return None
    fields[11] = unescape_name(fields[11])
    return fields


def stable_inode(key, used):
    """Inode number derived from ``key``, the same in every mount.

    restic recognizes unchanged files by path, size, times and inode, so a
    file that is identical in two backups should get the same number.
    ``used`` maps inode -> key; collisions (astronomically rare) are
    resolved by probing.
    """
    ino = int.from_bytes(hashlib.blake2b(key, digest_size=8).digest(),
                         'little') >> 1
    while ino <= ROOT_INODE or (ino in used and used[ino] != key):
        ino = (ino + 1) & ((1 << 63) - 1)
    used[ino] = key
    return ino


class Backup:
    """All entries of one storeBackup backup directory."""

    def __init__(self, path, allow_unfinished=False):
        self.path = os.path.abspath(path)
        info_file = os.path.join(self.path, CHECKSUM_FILE + '.info')
        if not os.path.isfile(info_file):
            raise BackupError(f'{self.path} is not a storeBackup backup '
                              f'(no {CHECKSUM_FILE}.info)')
        if os.path.exists(os.path.join(self.path, UNRESOLVED_LINKS)):
            raise BackupError(f'{self.path} has unresolved lateLinks, run '
                              'storeBackupUpdateBackup.pl first')
        self.info = parse_info(info_file)
        if not allow_unfinished and not self._finished():
            raise BackupError(f'{self.path} was not finished')
        self.postfix = (self.info.get('postfix') or [''])[0]
        self.uncompress = self.info.get('uncompress') or ['bzip2', '-d']
        self.archive_types = ''.join(self.info.get('archiveTypes') or [])
        self.root = Entry(DIR, b'', b'')
        self.root.inode = ROOT_INODE
        self.by_inode = {ROOT_INODE: self.root}
        self.counts = {}
        self.malformed = 0          # lines of the checksum file skipped
        # inode -> error of files whose contents are known to be wrong, and
        # inodes of files read completely with the right checksum
        self.bad = {}
        self.verified = set()
        self._load()

    def _finished(self):
        """Same rule as storeBackup's checkIfBackupWasFinished."""
        if os.path.exists(os.path.join(self.path, FINISHED_FLAG)):
            return True
        version = parse_version((self.info.get('storeBackupVersion')
                                 or [''])[0])
        if version > (3, 4, 3):
            return False
        return not os.path.exists(os.path.join(self.path, NOT_FINISHED_FLAG))

    # -- loading -----------------------------------------------------------

    def _checksum_lines(self):
        plain = os.path.join(self.path, CHECKSUM_FILE)
        if os.path.isfile(plain + '.bz2'):
            f = bz2.open(plain + '.bz2', 'rb')
        elif os.path.isfile(plain):
            f = open(plain, 'rb')
        else:
            raise BackupError(f'{self.path}: no {CHECKSUM_FILE}[.bz2]')
        with f:
            yield from f

    def _dir_for(self, path):
        """Return the directory entry for ``path``, creating missing parents
        (storeBackup does not list the parents of includeDirs)."""
        node = self.root
        if not path:
            return node
        for part in path.split(b'/'):
            child = node.children.get(part)
            if child is None:
                sub = part if node is self.root else node.path + b'/' + part
                child = Entry(DIR, part, sub)
                child.parent = node
                node.children[part] = child
            elif child.kind != DIR:
                raise BackupError(f'{self.path}: {child.path!r} is listed as '
                                  f'{child.kind} and as parent directory')
            node = child
        return node

    def _load(self):
        used = {ROOT_INODE: b'/'}
        links = {}
        for line in self._checksum_lines():
            fields = parse_checksum_line(line)
            if fields is not None:
                try:
                    ctime, mtime, atime, size, uid, gid, mode = (
                        int(f) for f in fields[4:11])
                except ValueError:
                    fields = None
            if fields is None:
                if line.strip() and not line.startswith(b'#'):
                    self.malformed += 1
                    if self.malformed <= 5:
                        log.warning('%s: malformed line in %s: %r',
                                    self.path, CHECKSUM_FILE, line)
                continue
            md5, compr, dev_inode, name = fields[0], fields[1], fields[2], \
                fields[11]
            name = name.strip(b'/')
            if name:
                check_relative_path(name, f'{self.path}: name')
            md5 = md5.decode('ascii', 'replace')
            if len(md5) == 32:
                kind = FILE
            elif md5 in _TYPE_BITS and md5 != FILE:
                kind = md5
            else:
                raise BackupError(f'{self.path}: unknown entry type {md5!r} '
                                  f'for {name!r}')
            if not name:            # the backup root itself
                entry = self.root
            elif kind == DIR:
                entry = self._dir_for(name)
            else:
                head, _, tail = name.rpartition(b'/')
                parent = self._dir_for(head)
                if tail in parent.children:
                    raise BackupError(f'{self.path}: {name!r} listed twice')
                entry = Entry(kind, tail, name)
                entry.parent = parent
                parent.children[tail] = entry
            entry.compr = compr.decode('ascii', 'replace')
            entry.md5 = md5 if kind == FILE else None
            entry.dev_inode = dev_inode
            entry.ctime, entry.mtime, entry.atime = ctime, mtime, atime
            entry.size = size if kind == FILE else 0
            entry.uid, entry.gid, entry.mode = uid, gid, mode
            self.counts[kind] = self.counts.get(kind, 0) + 1

            if entry is self.root:
                continue
            if kind == DIR:
                key = b'd\0' + name
            elif kind == FILE:
                # same content and same source inode -> same number in every
                # backup; source hard links share it as well
                key = b'f\0' + md5.encode() + b'\0' + dev_inode
                links[key] = links.get(key, 0) + 1
            else:
                key = kind.encode() + b'\0' + dev_inode + b'\0' + name
            entry.inode = stable_inode(key, used)
            self.by_inode[entry.inode] = entry

        if self.malformed:
            log.warning('%s: %d malformed lines in %s skipped', self.path,
                        self.malformed, CHECKSUM_FILE)
        self._finish_dirs(self.root, used)
        for entry in self.walk():
            if entry.kind == FILE:
                entry.nlink = links[b'f\0' + entry.md5.encode() + b'\0'
                                    + entry.dev_inode]

    def _finish_dirs(self, root, used):
        """Give implicit parent directories an inode and the backup's date,
        and count subdirectories for nlink."""
        stack = [root]
        st = os.stat(self.path)
        while stack:
            node = stack.pop()
            if node.inode is None:
                node.inode = stable_inode(b'd\0' + node.path, used)
                self.by_inode[node.inode] = node
            if node.compr is None:      # not listed in the checksum file
                node.mtime = node.ctime = node.atime = int(st.st_mtime)
            subdirs = [c for c in node.children.values() if c.kind == DIR]
            node.nlink = 2 + len(subdirs)
            stack.extend(subdirs)

    # -- access ------------------------------------------------------------

    def lookup(self, path):
        """Entry for a path relative to the backup root (bytes or str)."""
        if isinstance(path, str):
            path = os.fsencode(path)
        node = self.root
        for part in path.strip(b'/').split(b'/'):
            if not part:
                continue
            if node.kind != DIR or part not in node.children:
                raise KeyError(path)
            node = node.children[part]
        return node

    def walk(self):
        """All entries below the root, depth first, sorted by name."""
        stack = [iter(sorted(self.root.children.items()))]
        while stack:
            for _name, child in stack[-1]:
                yield child
                if child.kind == DIR:
                    stack.append(iter(sorted(child.children.items())))
                    break
            else:
                stack.pop()

    def stored_path(self, entry):
        """Absolute path of the stored file, directory of blocks or link."""
        path = os.path.join(os.fsencode(self.path), entry.path)
        if entry.kind == FILE and entry.compr == 'c':
            path += os.fsencode(self.postfix)
        return path

    def readlink(self, entry):
        if entry.kind != SYMLINK:
            raise BackupError(f'{entry.path!r} is no symlink')
        return os.readlink(self.stored_path(entry))

    def rdev(self, entry):
        """Device number of a device file (stored as device node by cp -a)."""
        if entry.kind not in (BLOCKDEV, CHARDEV):
            return 0
        try:
            st = os.lstat(self.stored_path(entry))
        except OSError:
            return 0
        return st.st_rdev if stat.S_ISBLK(st.st_mode) or \
            stat.S_ISCHR(st.st_mode) else 0

    def open(self, entry, verify=True):
        """Reader for the contents of a regular file.

        With ``verify``, files found damaged before are refused, and the
        returned reader checks the contents (see :class:`VerifyingReader`).
        """
        if entry.kind != FILE:
            raise BackupError(f'{entry.path!r} is no regular file')
        if verify and entry.inode in self.bad:
            raise ChecksumError(self.bad[entry.inode])
        path = self.stored_path(entry)
        try:
            if entry.compr == 'u':
                reader = PlainReader(path, entry.size)
            elif entry.compr == 'c':
                reader = StreamReader(lambda: self._decompressed(path),
                                      entry.size)
            elif entry.compr == 'b':
                reader = BlockReader(self, path, entry.size, verify=verify)
            else:
                raise BackupError(f'{entry.path!r}: unknown compr flag '
                                  f'{entry.compr!r}')
        except ChecksumError as e:
            if verify:
                self.mark_bad(entry, f'{entry.path!r}: {e}')
            raise
        return VerifyingReader(reader, entry, self) if verify else reader

    def mark_bad(self, entry, error):
        self.bad.setdefault(entry.inode, error)
        self.verified.discard(entry.inode)

    def decompress_file(self, path):
        """Whole decompressed contents of ``path`` (one call into the
        library, which releases the GIL; much faster than reading a stream)."""
        prog = os.path.basename(self.uncompress[0])
        with open(path, 'rb') as f:
            if prog in ('bzip2', 'bunzip2', 'lbzip2', 'pbzip2'):
                return bz2.decompress(f.read())
            if prog in ('gzip', 'gunzip', 'pigz'):
                return gzip.decompress(f.read())
            if prog in ('xz', 'unxz', 'lzma', 'unlzma'):
                return lzma.decompress(f.read())
            proc = subprocess.run(self.uncompress, stdin=f,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
        if proc.returncode:
            raise BackupError(f'{shlex.join(self.uncompress)} failed with '
                              f'status {proc.returncode} on {path!r}')
        return proc.stdout

    def _decompressed(self, path):
        """Binary stream of the decompressed contents of ``path``."""
        prog = os.path.basename(self.uncompress[0])
        if prog in ('bzip2', 'bunzip2', 'lbzip2', 'pbzip2'):
            return bz2.open(path, 'rb')
        if prog in ('gzip', 'gunzip', 'pigz'):
            return gzip.open(path, 'rb')
        if prog in ('xz', 'unxz', 'lzma', 'unlzma'):
            return lzma.open(path, 'rb')
        return _ProcessStream(self.uncompress, path)


# -- readers ---------------------------------------------------------------
#
# The kernel sends the reads of one open file in parallel; ``read`` may be
# called from several threads at once and serializes on ``lock``.
# ``read_nowait`` is for the event loop: it returns None instead of waiting
# for the lock or for data that is not at hand.

class Reader:

    def __init__(self):
        self.lock = threading.Lock()

    def read(self, offset, length):
        with self.lock:
            return self._read(offset, length)

    def read_nowait(self, offset, length):
        if not self.lock.acquire(blocking=False):
            return None
        try:
            if not self._ready(offset, length):
                return None
            return self._read(offset, length)
        finally:
            self.lock.release()

    def close(self):
        with self.lock:
            self._close()

    def _ready(self, offset, length):
        return False

    def _close(self):
        pass


class PlainReader(Reader):
    """File stored without compression."""

    def __init__(self, path, size):
        super().__init__()
        self.size = size
        self.fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        stored = os.fstat(self.fd).st_size
        if stored != size:
            os.close(self.fd)
            raise ChecksumError(f'stored file has {stored} bytes, the '
                                f'checksum file says {size}')

    def _ready(self, offset, length):
        return True

    def _read(self, offset, length):
        return os.pread(self.fd, length, offset)

    def _close(self):
        os.close(self.fd)


class StreamReader(Reader):
    """Random access on top of a decompressing stream.

    Sequential reads (the normal case for backup programs) cost one pass;
    a read before the current position restarts the stream.
    """

    CHUNK = 1 << 20

    def __init__(self, open_stream, size):
        super().__init__()
        self.open_stream = open_stream
        self.size = size
        self.stream = None
        self.pos = 0

    def _ready(self, offset, length):
        """True if the read continues where the last one ended."""
        return self.stream is not None and offset == self.pos

    def _read(self, offset, length):
        length = min(length, self.size - offset)
        if length <= 0:
            return b''
        if self.stream is None or offset < self.pos:
            self._close()
            self.stream = self.open_stream()
            self.pos = 0
        while self.pos < offset:
            skipped = self.stream.read(min(self.CHUNK, offset - self.pos))
            if not skipped:
                break
            self.pos += len(skipped)
        data = self.stream.read(length) if self.pos == offset else b''
        self.pos += len(data)
        if len(data) < length:
            raise ChecksumError(f'stored file ends after {self.pos} '
                                f'bytes, the checksum file says {self.size}')
        return data

    def _close(self):
        if self.stream is not None:
            self.stream.close()
            self.stream = None


class BlockReader(Reader):
    """File stored by checkBlocks: a directory with the file's blocks.

    ``.md5BlockCheckSums.bz2`` in that directory lists ``md5 compr entry``
    per block, ``entry`` relative to the backup root.  All blocks have the
    size of the first one, except the last.  The blocks following the one
    being read are decompressed in parallel (bz2 releases the GIL).  With
    ``verify``, each block is checked against its MD5 when it is loaded,
    independent of the order of the reads.
    """

    def __init__(self, backup, path, size, verify=True, prefetch=4):
        super().__init__()
        self.backup = backup
        self.path = path
        self.size = size
        self.verify = verify
        self.blocks = []
        with bz2.open(os.path.join(path, os.fsencode(BLOCK_CHECKSUM_FILE)),
                      'rb') as f:
            for line in f:
                parts = line.rstrip(b'\n').split(b' ', 2)
                if len(parts) != 3:
                    raise BackupError(f'{path!r}: bad line {line!r} in '
                                      f'{BLOCK_CHECKSUM_FILE}')
                check_relative_path(parts[2], f'{path!r}: block')
                self.blocks.append((parts[0].decode('ascii', 'replace'),
                                    parts[1].decode('ascii', 'replace'),
                                    parts[2]))
        self.prefetch = prefetch
        self.pool = None
        self.pending = {}           # block index -> Future
        self.cache_index = None
        self.cache_data = b''
        self.block_size = 0
        try:
            self.block_size = len(self._block(0)) if self.blocks else 0
            if self.size and not self.block_size:
                raise BackupError(f'{path!r}: first block is empty')
        except BaseException:
            self._close()
            raise

    def _load(self, index):
        """Contents of one block; runs in the prefetch threads."""
        md5, compr, entry = self.blocks[index]
        path = os.path.join(os.fsencode(self.backup.path), entry)
        if compr == 'c':
            data = self.backup.decompress_file(path)
        else:
            with open(path, 'rb') as f:
                data = f.read()
        if self.verify and hashlib.md5(data).hexdigest() != md5:
            raise ChecksumError(f'block {entry!r} does not match '
                                f'{BLOCK_CHECKSUM_FILE}')
        return data

    def _block(self, index):
        if index == self.cache_index:
            return self.cache_data
        future = self.pending.pop(index, None)
        data = future.result() if future else self._load(index)
        if self.block_size and index < len(self.blocks) - 1 and \
                len(data) != self.block_size:
            raise BackupError(f'{self.path!r}: block {index} has '
                              f'{len(data)} bytes instead of '
                              f'{self.block_size}')
        for i in [i for i in self.pending if i < index]:
            self.pending.pop(i).cancel()
        if self.prefetch:
            if self.pool is None:
                self.pool = concurrent.futures.ThreadPoolExecutor(
                    self.prefetch, thread_name_prefix='block')
            for i in range(index + 1,
                           min(index + 1 + self.prefetch, len(self.blocks))):
                if i not in self.pending:
                    self.pending[i] = self.pool.submit(self._load, i)
        self.cache_index, self.cache_data = index, data
        return data

    def _ready(self, offset, length):
        """True if the blocks for this read are decompressed already."""
        end = min(offset + length, self.size)
        if offset >= end:
            return True
        for index in range(offset // self.block_size,
                           (end - 1) // self.block_size + 1):
            if index != self.cache_index:
                future = self.pending.get(index)
                if future is None or not future.done():
                    return False
        return True

    def _read(self, offset, length):
        out = []
        end = min(offset + length, self.size)
        while offset < end:
            index, start = divmod(offset, self.block_size)
            if index >= len(self.blocks):
                break               # fewer blocks than the size says
            data = self._block(index)[start:start + end - offset]
            if not data:
                if index < len(self.blocks) - 1:
                    raise BackupError(f'{self.path!r}: block {index} is '
                                      'too short')
                break
            out.append(data)
            offset += len(data)
        return b''.join(out)

    def stored_size(self):
        """Size of all blocks together."""
        if not self.blocks:
            return 0
        last = len(self.blocks) - 1
        return last * self.block_size + len(self._load(last))

    def _close(self):
        for future in self.pending.values():
            future.cancel()
        self.pending = {}
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)
        self.cache_data = b''


class VerifyingReader(Reader):
    """Checks the size and MD5 that the checksum file records for a file.

    The check runs when the file is read completely, in any order: data read
    ahead of a gap is kept until the gap is filled (up to ``MAX_AHEAD``
    bytes; files read in a more scattered way are not checked).  A mismatch
    raises :class:`ChecksumError` on the read that completes the file and on
    every later read, and is recorded in ``backup.bad`` so that the file
    cannot be opened again; a match is recorded in ``backup.verified``.
    """

    MAX_AHEAD = 64 << 20

    def __init__(self, reader, entry, backup):
        super().__init__()
        self.reader = reader
        self.entry = entry
        self.backup = backup
        self.size = reader.size
        self.md5 = hashlib.md5()
        self.pos = 0                # contents up to here are hashed
        self.ahead = {}             # offset -> data read beyond pos
        self.ahead_bytes = 0
        self.done = entry.inode in backup.verified
        self.error = None

    def read(self, offset, length):
        with self.lock:
            return self._checked(offset, self.reader.read, length)

    def read_nowait(self, offset, length):
        if not self.lock.acquire(blocking=False):
            return None
        try:
            return self._checked(offset, self.reader.read_nowait, length)
        finally:
            self.lock.release()

    def _checked(self, offset, read, length):
        if self.error:
            raise ChecksumError(self.error)
        try:
            data = read(offset, length)
        except ChecksumError as e:
            self._fail(f'{self.entry.path!r}: {e}')
        if data is not None and not self.done:
            self._account(offset, data)
        return data

    def _account(self, offset, data):
        if offset > self.pos:
            if self.ahead_bytes + len(data) > self.MAX_AHEAD:
                log.warning('%r read too far out of order, not checked',
                            self.entry.path)
                self.done = True
                self.ahead = {}
                return
            if len(data) > len(self.ahead.get(offset, b'')):
                self.ahead_bytes += len(data) - len(self.ahead.get(offset,
                                                                    b''))
                self.ahead[offset] = data
        else:
            self._hash(offset, data)
        while self.ahead:
            ready = [o for o in self.ahead if o <= self.pos]
            if not ready:
                break
            for o in ready:
                chunk = self.ahead.pop(o)
                self.ahead_bytes -= len(chunk)
                self._hash(o, chunk)
        if self.pos >= self.size or (not data and offset == self.pos):
            self._finish()

    def _hash(self, offset, data):
        if offset + len(data) > self.pos:
            self.md5.update(data[self.pos - offset:])
            self.pos = offset + len(data)

    def _finish(self):
        self.done = True
        self.ahead = {}
        if self.pos == self.size and self.md5.hexdigest() == self.entry.md5:
            self.backup.verified.add(self.entry.inode)
            return
        stored_size = getattr(self.reader, 'stored_size', None)
        if stored_size is not None:
            # storeBackup records the size of a file in blocks from before
            # reading it, but the MD5 of what it read (storeBackup.pl warns
            # "changed during backup"); the blocks themselves are checked
            stored = stored_size()
            if stored != self.size:
                log.warning('%r changed while storeBackup read it: '
                            '%d bytes recorded, %d stored; shown with the '
                            'recorded size', self.entry.path, self.size,
                            stored)
                return
        self._fail(f'{self.entry.path!r}: contents do not match the '
                   f'checksum file (size {self.pos}/{self.size})')

    def _fail(self, error):
        self.done = True
        self.ahead = {}
        self.error = error
        self.backup.mark_bad(self.entry, error)
        raise ChecksumError(error)

    def _close(self):
        self.reader.close()


class _ProcessStream:
    """Decompression by an external program (uncompress= of the backup)."""

    def __init__(self, command, path):
        self.command = command
        self.file = open(path, 'rb')
        self.proc = subprocess.Popen(command, stdin=self.file,
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL)

    def read(self, n=-1):
        data = self.proc.stdout.read(n)
        if n and (n < 0 or not data) and self.proc.wait():
            raise BackupError(f'{shlex.join(self.command)} failed with '
                              f'status {self.proc.returncode}')
        return data

    def close(self):
        self.proc.stdout.close()
        self.proc.kill()
        self.proc.wait()
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
