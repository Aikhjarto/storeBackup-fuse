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
import collections
import concurrent.futures
import errno
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
import time
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


# uncompress= programs replaced by Python's own modules
BUILTIN_CODECS = {}
for _names, _module in ((('bzip2', 'bunzip2', 'lbzip2', 'pbzip2'), bz2),
                        (('gzip', 'gunzip', 'pigz'), gzip),
                        (('xz', 'unxz', 'lzma', 'unlzma'), lzma)):
    BUILTIN_CODECS.update(dict.fromkeys(_names, _module))

# errors of the decompression libraries that are no OSError
DECOMPRESSION_ERRORS = (EOFError, lzma.LZMAError, zlib.error)


def is_data_error(e):
    """True if ``e`` from a decompression library means damaged data, not
    a failed read or a programming error: bz2 and gzip raise OSError
    without errno ("Invalid data stream", BadGzipFile), bz2.decompress
    ValueError for data cut off."""
    if isinstance(e, DECOMPRESSION_ERRORS):
        return True
    if isinstance(e, ValueError):
        return 'end-of-stream marker' in str(e)
    return isinstance(e, OSError) and e.errno is None


def program_failed(command, status, path=None):
    """Error for an uncompress program that failed.  Not taken as damage:
    the exit status does not tell damaged data from other failures."""
    where = f' on {path!r}' if path is not None else ''
    return BackupError(f'{shlex.join(command)} failed with status '
                       f'{status}{where}')


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


def read_info(path):
    """Parse ``.md5CheckSums.info`` into a dict of key -> list of words,
    and return it with the stat of the file that was read (not of its
    name, which may change meanwhile).  It must be a regular file.

    Values are written by storeBackup as shell-like quoted words, e.g.
    ``uncompress='bzip2' '-d'``.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        raise BackupError(f'{path}: {e.strerror}') from None
    with open(fd, 'rb') as f:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise BackupError(f'{path} is not a regular file')
        return _parse_info_file(f), st


def _parse_info_file(f):
    info = {}
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
    file that is identical in two backups should get the same number.  The
    source inode in the checksum file cannot serve for that: sources read
    over sshfs get new device and inode numbers with every mount.
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

    def __init__(self, path, allow_unfinished=False,
                 allow_external_uncompress=False):
        self.path = os.path.abspath(path)
        info_file = os.path.join(self.path, CHECKSUM_FILE + '.info')
        if not os.path.isfile(info_file):
            raise BackupError(f'{self.path} is not a storeBackup backup '
                              f'(no {CHECKSUM_FILE}.info)')
        if os.path.exists(os.path.join(self.path, UNRESOLVED_LINKS)):
            raise BackupError(f'{self.path} has unresolved lateLinks, run '
                              'storeBackupUpdateBackup.pl first')
        # owner and mode are checked below on the file that was read
        self.info, info_stat = read_info(info_file)
        if not allow_unfinished and not self._finished():
            raise BackupError(f'{self.path} was not finished')
        self.postfix = (self.info.get('postfix') or [''])[0]
        self.uncompress = self.info.get('uncompress') or ['bzip2', '-d']
        self.codec = BUILTIN_CODECS.get(os.path.basename(self.uncompress[0]))
        if self.codec is None and not allow_external_uncompress:
            # the program would run with our rights, usually root; the file
            # read must be the one in the backup, not reached through a
            # symbolic link to some file of root
            st = info_stat
            try:
                lst = os.lstat(info_file)
            except OSError:
                lst = None
            if lst is None or (lst.st_dev, lst.st_ino) != (st.st_dev,
                                                           st.st_ino):
                raise BackupError(
                    f'{info_file} is a symbolic link (or was replaced) and '
                    f'names the uncompress program '
                    f'{shlex.join(self.uncompress)}; not run unless allowed '
                    'explicitly')
            if st.st_uid not in (0, os.geteuid()) or st.st_mode & 0o022:
                raise BackupError(
                    f'{info_file} (owner uid {st.st_uid}, mode '
                    f'{stat.S_IMODE(st.st_mode):o}) names the uncompress '
                    f'program {shlex.join(self.uncompress)}; not run unless '
                    'allowed explicitly')
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
        links = {}                  # see _assign_file_inodes
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
            if kind == FILE:
                links.setdefault((md5, dev_inode, uid, gid, mode, mtime,
                                  size), []).append(entry)
                continue
            key = (b'd' if kind == DIR else kind.encode()) + b'\0' + name
            entry.inode = stable_inode(key, used)
            self.by_inode[entry.inode] = entry

        if self.malformed:
            log.warning('%s: %d malformed lines in %s skipped', self.path,
                        self.malformed, CHECKSUM_FILE)
        self._assign_file_inodes(links, used)
        self._finish_dirs(self.root, used)

    def _assign_file_inodes(self, links, used):
        """Inode numbers of regular files from contents and path.

        Hard links of the source are recognized by the source inode, which
        is reliable within one backup.  Entries are only taken as hard links
        if all attributes of the inode agree as well, so that two files
        which got the same inode number one after the other during the
        backup (e.g. temporary files) are not merged.  A group of hard links
        gets the number of its first path.
        """
        for (md5, *_), entries in links.items():
            first = min(e.path for e in entries)
            inode = stable_inode(b'f\0' + md5.encode() + b'\0' + first, used)
            for entry in entries:
                entry.inode = inode
                entry.nlink = len(entries)
            self.by_inode[inode] = entries[0]

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
        with open(path, 'rb') as f:
            if self.codec is not None:
                compressed = f.read()
                try:
                    return self.codec.decompress(compressed)
                except Exception as e:
                    if is_data_error(e):
                        raise ChecksumError(f'{path!r} cannot be '
                                            f'decompressed: {e}') from None
                    raise
            proc = subprocess.run(self.uncompress, stdin=f,
                                  stdout=subprocess.PIPE,
                                  stderr=subprocess.DEVNULL)
        if proc.returncode:
            raise program_failed(self.uncompress, proc.returncode, path)
        return proc.stdout

    def _decompressed(self, path):
        """Binary stream of the decompressed contents of ``path``."""
        if self.codec is not None:
            return self.codec.open(path, 'rb')
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

    nowait_supported = True

    def __init__(self, path, size):
        super().__init__()
        self.size = size
        self.fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        stored = os.fstat(self.fd).st_size
        if stored != size:
            os.close(self.fd)
            raise ChecksumError(f'stored file has {stored} bytes, the '
                                f'checksum file says {size}')

    def read_nowait(self, offset, length):
        """Data only if it is in the page cache (no disk access in the
        event loop); pread needs no lock."""
        length = min(length, self.size - offset)
        if length <= 0:
            return b''
        if not PlainReader.nowait_supported:
            return None
        buf = bytearray(length)
        try:
            n = os.preadv(self.fd, [buf], offset, os.RWF_NOWAIT)
        except BlockingIOError:
            return None
        except OSError as e:
            if e.errno in (errno.EOPNOTSUPP, errno.EINVAL):
                PlainReader.nowait_supported = False
                return None
            raise
        return bytes(memoryview(buf)[:n]) if n == length else None

    def _read(self, offset, length):
        return os.pread(self.fd, length, offset)

    def _close(self):
        os.close(self.fd)


class _Decompressor:
    """Thread decompressing a stream from ``start`` on into a buffer.

    It stays ahead of the furthest read by a step that starts at one chunk
    and doubles while reads keep up with it, up to ``ahead`` bytes, so a
    short read of a file costs little.  It keeps ``behind`` bytes before
    the furthest read, as the kernel's parallel reads of one file arrive
    slightly out of order, and nothing older, also while it skips forward
    to a far read.
    """

    def __init__(self, open_stream, start, size, chunk, ahead, behind):
        self.cond = threading.Condition()
        self.chunks = collections.deque()
        self.begin = self.end = start   # buffered: [begin, end)
        self.wanted = start             # furthest offset read
        self.needed = start             # furthest end of a read
        self.ahead, self.behind = ahead, behind
        self.step = min(chunk, ahead)   # current read-ahead
        self.eof = False
        self.error = None
        self.stopped = False
        threading.Thread(target=self._run, daemon=True, name='decompress',
                         args=(open_stream, start, size, chunk)).start()

    def _run(self, open_stream, start, size, chunk):
        try:
            stream = open_stream()
            try:
                pos = 0
                while pos < start and not self.stopped:
                    skipped = stream.read(min(chunk, start - pos))
                    if not skipped:
                        break
                    pos += len(skipped)
                if pos < start:
                    with self.cond:     # the stream ends before start
                        self.begin = self.end = pos
                    return
                while True:
                    with self.cond:
                        while not self.stopped and \
                                self.end >= self.needed and \
                                self.end - self.wanted >= self.step:
                            self.cond.wait()
                        if self.stopped:
                            return
                    data = stream.read(min(chunk, size - self.end))
                    self._put(data)
                    if not data or self.end >= size:
                        return
            finally:
                stream.close()
        except BaseException as e:
            with self.cond:
                self.error = e
                self.cond.notify_all()
        finally:
            with self.cond:
                self.eof = True
                self.cond.notify_all()

    def _put(self, data):
        with self.cond:
            if data:
                self.chunks.append(data)
                self.end += len(data)
                self._trim()
            self.cond.notify_all()

    def _trim(self):
        """Forget what lies well before the furthest read."""
        while self.chunks and \
                self.begin + len(self.chunks[0]) <= self.wanted - self.behind:
            self.begin += len(self.chunks.popleft())

    def ready(self, offset, length):
        return self.begin <= offset and offset + length <= self.end

    def get(self, offset, length, wait=True):
        """Data at ``offset`` (shorter at the end of the stream), or None
        if it is not there yet and ``wait`` is false."""
        with self.cond:
            if offset > self.wanted or offset + length > self.needed:
                self.wanted = max(self.wanted, offset)
                self.needed = max(self.needed, offset + length)
                self.cond.notify_all()
            # sequential reads catch up with the read-ahead (seeks beyond
            # the buffer do not count): read further ahead
            if self.begin < self.end and self.step < self.ahead and \
                    self.begin <= offset <= self.end and \
                    offset + length > self.end - self.step // 2:
                self.step = min(2 * self.step, self.ahead)
                self.cond.notify_all()
            while offset + length > self.end and not self.eof:
                if not wait:
                    return None
                self.cond.wait()
            if self.error is not None and offset + length > self.end:
                raise self.error
            out, pos = [], self.begin
            for chunk in self.chunks:
                if pos + len(chunk) > offset:
                    out.append(chunk[max(offset - pos, 0):
                                     offset + length - pos])
                pos += len(chunk)
                if pos >= offset + length:
                    break
            self._trim()
            return b''.join(out)

    def stop(self):
        with self.cond:
            self.stopped = True
            self.chunks.clear()
            self.cond.notify_all()


class StreamReader(Reader):
    """Random access on top of a decompressing stream.

    A thread decompresses ahead of the reads (see :class:`_Decompressor`),
    so several files are decompressed in parallel and the event loop never
    decompresses.  A read before the buffered data restarts the stream.
    """

    CHUNK = 1 << 20
    AHEAD = 8 << 20
    BEHIND = 4 << 20
    LEAD = 1 << 20
    RETRY_AFTER = 30                # seconds

    def __init__(self, open_stream, size):
        super().__init__()
        self.open_stream = open_stream
        self.size = size
        self.decompressor = None
        self.failed = None          # (error, time) of the last read error

    def _ready(self, offset, length):
        length = min(length, self.size - offset)
        return length <= 0 or (self.decompressor is not None and
                               self.decompressor.ready(offset, length))

    def _read(self, offset, length):
        length = min(length, self.size - offset)
        if length <= 0:
            return b''
        if self.failed is not None:
            error, when = self.failed
            if time.monotonic() - when < self.RETRY_AFTER:
                raise error
            self.failed = None
        if self.decompressor is None or offset < self.decompressor.begin:
            self._close()
            # start a bit before: parallel reads arrive in any order, and
            # the first one need not be the lowest
            self.decompressor = _Decompressor(
                self.open_stream, max(offset - self.LEAD, 0), self.size,
                self.CHUNK, self.AHEAD, self.BEHIND)
        try:
            data = self.decompressor.get(offset, length)
        except Exception as e:
            self._close()
            if is_data_error(e):
                raise ChecksumError(f'cannot be decompressed: {e}') from None
            # maybe passing: later reads fail at once for a while, then
            # the stream starts again (not for each read: that would
            # decompress up to the failing point every time)
            self.failed = (e, time.monotonic())
            raise
        if len(data) < length:
            raise ChecksumError(f'stored file ends after '
                                f'{self.decompressor.end} bytes, the '
                                f'checksum file says {self.size}')
        return data

    def _close(self):
        if self.decompressor is not None:
            self.decompressor.stop()
            self.decompressor = None


class BlockReader(Reader):
    """File stored by checkBlocks: a directory with the file's blocks.

    ``.md5BlockCheckSums.bz2`` in that directory lists ``md5 compr entry``
    per block, ``entry`` relative to the backup root.  All blocks have the
    size of the first one, except the last.  The blocks following the one
    being read are decompressed in parallel (bz2 releases the GIL).  With
    ``verify``, each block is checked against its MD5 when it is loaded,
    independent of the order of the reads.

    If the blocks hold less than the recorded size (the file shrank while
    storeBackup read it), the rest reads as zeros, as with
    storeBackupRecover.pl.
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
        # the last blocks read: reads of one file arrive slightly out of
        # order, also across block boundaries
        self.cache = {}
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

    # blocks kept after reading them: at least 2, and enough for 2 MB, as
    # the kernel's parallel reads (up to 1 MB each) arrive out of order
    CACHE_BYTES = 2 << 20

    def _fetch(self, index):
        """Contents of a block not in the cache, from the prefetch threads
        or loaded now, with its length checked."""
        future = self.pending.pop(index, None)
        data = future.result() if future else self._load(index)
        if self.block_size and index < len(self.blocks) - 1 and \
                len(data) != self.block_size:
            raise ChecksumError(f'{self.path!r}: block {index} has '
                              f'{len(data)} bytes instead of '
                              f'{self.block_size}')
        return data

    def _prefetch(self, index):
        """Load the blocks following ``index`` in the background, and stop
        loading others."""
        window = range(index + 1,
                       min(index + 1 + self.prefetch, len(self.blocks)))
        for i in [i for i in self.pending if i not in window]:
            self.pending.pop(i).cancel()
        if not self.prefetch:
            return
        if self.pool is None:
            self.pool = concurrent.futures.ThreadPoolExecutor(
                self.prefetch, thread_name_prefix='block')
        for i in window:
            if i not in self.pending and i not in self.cache:
                self.pending[i] = self.pool.submit(self._load, i)

    def _block(self, index):
        if index in self.cache:     # forgotten in the order loaded
            return self.cache[index]
        data = self._fetch(index)
        self._prefetch(index)
        self._remember(index, data)
        return data

    def _remember(self, index, data):
        self.cache[index] = data
        cached = max(2, -(-self.CACHE_BYTES // max(self.block_size, 1)))
        while len(self.cache) > cached:
            del self.cache[next(iter(self.cache))]

    def _ready(self, offset, length):
        """True if the blocks for this read are decompressed already."""
        end = min(offset + length, self.size)
        if offset >= end:
            return True
        for index in range(offset // self.block_size,
                           min((end - 1) // self.block_size + 1,
                               len(self.blocks))):
            if index not in self.cache:
                future = self.pending.get(index)
                if future is None or not future.done():
                    return False
        return True

    def _read(self, offset, length):
        out = []
        end = min(offset + length, self.size)
        while offset < end:
            index, start = divmod(offset, self.block_size)
            data = b''
            if index < len(self.blocks):
                data = self._block(index)[start:start + end - offset]
            if not data:            # beyond the blocks (all but the last
                out.append(bytes(end - offset))     # have block_size)
                break
            out.append(data)
            offset += len(data)
        return b''.join(out)

    def tail_start(self):
        """Offset of the first block that may differ from what reads up to
        the recorded size return: the last stored block, or the block
        holding the recorded end if more blocks follow."""
        if not self.blocks or not self.block_size:
            return 0
        return min(len(self.blocks) - 1,
                   self.size // self.block_size) * self.block_size

    def stored_md5(self, md5, start):
        """Size and MD5 of all blocks together (each block is checked),
        given the MD5 ``md5`` of the contents before ``start``, which
        :meth:`tail_start` returned."""
        md5 = md5.copy()
        size = start
        first = start // self.block_size if self.block_size else 0
        for index in range(first, len(self.blocks)):
            with self.lock:
                data = self.cache.get(index)
                if data is None:
                    data = self._fetch(index)
                    self._remember(index, data)
                self._prefetch(index)
            md5.update(data)
            size += len(data)
        return size, md5.hexdigest()

    def _close(self):
        for future in self.pending.values():
            future.cancel()
        self.pending = {}
        if self.pool is not None:
            self.pool.shutdown(wait=False, cancel_futures=True)
        self.cache.clear()


class VerifyingReader(Reader):
    """Checks the size and MD5 that the checksum file records for a file.

    The check runs when the file has been read completely by this reader,
    in any order: data read ahead of a gap is kept until the gap is filled
    (up to ``MAX_AHEAD`` bytes; files read in a more scattered way are not
    checked).  A mismatch raises :class:`ChecksumError` on the read that
    completes the file and on every later read, and is recorded in
    ``backup.bad``, which makes every reader of the file fail from then on;
    a match is recorded in ``backup.verified``.

    Each reader must see all reads of its file: the filesystem does not
    share the page cache between readers of a file that is not verified.
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
        self.end_read = False       # the read reaching the end was seen
        # for files in blocks: MD5 of the contents before tail_start, so
        # that the MD5 of the stored blocks costs only the blocks after it
        tail_start = getattr(reader, 'tail_start', None)
        self.tail_start = tail_start() if tail_start else None
        self.tail_md5 = hashlib.md5() if self.tail_start == 0 else None

    def read(self, offset, length):
        with self.lock:
            return self._checked(offset, self.reader.read, length)

    def read_nowait(self, offset, length):
        if not self.lock.acquire(blocking=False):
            return None
        try:
            if self._may_finish(offset, length):
                return None
            return self._checked(offset, self.reader.read_nowait, length)
        finally:
            self.lock.release()

    def _may_finish(self, offset, length):
        """True if this read may complete the check, which may take long:
        then not in the event loop.  For files in blocks, a read filling
        the last gap may make it read blocks again."""
        if self.done:
            return False
        if offset + length >= self.size:
            return True
        return self.end_read and self.tail_start is not None and \
            offset <= self.pos < offset + length

    def _checked(self, offset, read, length):
        if self.error is None and self.entry.inode in self.backup.bad:
            self.error = self.backup.bad[self.entry.inode]
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
        if offset + len(data) >= self.size or not data:
            self.end_read = True
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
        end = offset + len(data)
        if end > self.pos:
            cut = self.tail_start
            if self.tail_md5 is None and cut is not None and \
                    self.pos <= cut <= end:
                self.md5.update(data[self.pos - offset:cut - offset])
                self.tail_md5 = self.md5.copy()
                self.md5.update(data[cut - offset:])
            else:
                self.md5.update(data[self.pos - offset:])
            self.pos = end

    def _finish(self):
        self.done = True
        self.ahead = {}
        if self.pos == self.size and self.md5.hexdigest() == self.entry.md5:
            self.backup.verified.add(self.entry.inode)
            return
        stored_md5 = getattr(self.reader, 'stored_md5', None)
        # tail_md5 is set when pos passed tail_start (<= size)
        if stored_md5 is not None and self.tail_md5 is not None:
            # storeBackup records the size of a file in blocks from before
            # reading it, but the MD5 of what it read (storeBackup.pl warns
            # "changed during backup")
            try:
                stored, md5 = stored_md5(self.tail_md5, self.tail_start)
            except ChecksumError as e:
                self._fail(f'{self.entry.path!r}: {e}')
            except Exception as e:
                # no proof of damage (EIO, missing block file, no memory,
                # uncompress program failed): this read fails, the file stays readable; each
                # block was checked against its own MD5 already
                log.warning('%r: not checked as a whole, reading the stored '
                            'blocks failed: %s', self.entry.path, e)
                raise
            if md5 == self.entry.md5 and stored != self.size:
                log.warning('%r changed while storeBackup read it: %d bytes '
                            'recorded, %d stored; shown with the recorded '
                            'size%s', self.entry.path, self.size, stored,
                            ', filled up with zeros' if stored < self.size
                            else '')
                self.backup.verified.add(self.entry.inode)
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
            raise program_failed(self.command, self.proc.returncode)
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
