# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Thomas Wagner
import bz2
import os
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'src'))

STOREBACKUP = shutil.which('storeBackup.pl')

needs_storebackup = pytest.mark.skipif(
    STOREBACKUP is None, reason='storeBackup.pl not installed')


# -- synthetic backups -----------------------------------------------------

INFO = """version=1.3
compress='bzip2'
postfix='.bz2'
archiveTypes=
"""

HEADER = (b'# contents/md5 compr dev-inode inodeBackup ctime mtime atime '
          b'size uid gid mode filename\n')


class SyntheticBackup:
    """Writes a backup directory in storeBackup's layout by hand."""

    def __init__(self, path, finished=True, version='3.5.2',
                 uncompress="'bzip2' '-d'"):
        self.path = path
        self.lines = []
        os.makedirs(path)
        with open(os.path.join(path, '.md5CheckSums.info'), 'w') as f:
            f.write(INFO + f'uncompress={uncompress}\n')
            if version:
                f.write(f'storeBackupVersion={version}\n')
        if finished:
            os.makedirs(os.path.join(path, '.storeBackupLinks'))
            open(os.path.join(path, '.storeBackupLinks',
                              'backup.Finished'), 'w').close()
        self._ino = 100

    def _line(self, md5, compr, size, mode, name, uid=0, gid=0,
              mtime=1700000000, dev_inode=None):
        self._ino += 1
        dev_inode = dev_inode or f'1-{self._ino}'
        esc = name.replace(b'\\', b'\\5C').replace(b'\n', b'\\0A')
        self.lines.append(b'%s %s %s 0 %d %d %d %d %d %d %d %s\n' % (
            md5.encode(), compr.encode(), dev_inode.encode(), mtime, mtime,
            mtime, size, uid, gid, mode, esc))

    def _target(self, name):
        target = os.path.join(os.fsencode(self.path), name)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        return target

    def dir(self, name, mode=0o755, **kw):
        os.makedirs(self._target(name), exist_ok=True)
        self._line('dir', '0', 0, mode, name, **kw)

    def file(self, name, data, compr='u', mode=0o644, md5=None,
             compress=bz2.compress, **kw):
        import hashlib
        target = self._target(name)
        if compr == 'c':
            with open(target + b'.bz2', 'wb') as f:
                f.write(compress(data))
        else:
            with open(target, 'wb') as f:
                f.write(data)
        self._line(md5 or hashlib.md5(data).hexdigest(), compr, len(data),
                   mode, name, **kw)

    def blocked(self, name, data, block_size, compress=True, size=None,
                md5=None, **kw):
        """File in blocks; ``size`` and ``md5`` override what goes into
        the checksum file.  Returns the paths of the stored blocks."""
        import hashlib
        target = self._target(name)
        os.makedirs(target)
        lines, paths = [], []
        for i in range(0, max(len(data), 1), block_size):
            block = data[i:i + block_size]
            rel = name + b'/%010d' % (i // block_size + 1)
            if compress:
                rel += b'.bz2'
                block_data = bz2.compress(block)
            else:
                block_data = block
            paths.append(os.path.join(os.fsencode(self.path), rel))
            with open(paths[-1], 'wb') as f:
                f.write(block_data)
            lines.append(b'%s %s %s\n' % (hashlib.md5(block).hexdigest()
                                         .encode(),
                                         b'c' if compress else b'u', rel))
        with bz2.open(os.path.join(target, b'.md5BlockCheckSums.bz2'),
                      'wb') as f:
            f.writelines(lines)
        self._line(md5 or hashlib.md5(data).hexdigest(), 'b',
                   len(data) if size is None else size, 0o644, name, **kw)
        return paths

    def symlink(self, name, to, **kw):
        os.symlink(to, self._target(name))
        self._line('symlink', '0', 0, 0, name, **kw)

    def special(self, kind, name, mode=0o644, **kw):
        self._line(kind, 'u' if kind != 'pipe' else '0', 0, mode, name, **kw)

    def raw_line(self, line):
        self.lines.append(line)

    def write(self):
        with bz2.open(os.path.join(self.path, '.md5CheckSums.bz2'),
                      'wb') as f:
            f.write(HEADER)
            f.writelines(self.lines)
        return self.path


@pytest.fixture
def synthetic(tmp_path):
    def make(name='2026.01.01_00.00.00', **kw):
        return SyntheticBackup(str(tmp_path / 'series' / name), **kw)
    return make


# -- backups made by storeBackup.pl ----------------------------------------

def make_source(src):
    """Source tree covering the file types and naming cases."""
    src = os.fsencode(src)
    j = lambda *p: os.path.join(src, *p)
    os.makedirs(j(b'd', b'sub', b'empty dir'))
    with open(j(b'random.bin'), 'wb') as f:          # blocks, not compressed
        f.write(os.urandom(300000))
    with open(j(b'zeros.img'), 'wb') as f:           # blocks, compressed
        f.write(b'\0' * 100000 + b'tail' * 50000)
    with open(j(b'd', b'text.txt'), 'wb') as f:      # compressed file
        f.write(b'hello world\n' * 20000)
    with open(j(b'small'), 'wb') as f:
        f.write(b'x')
    os.link(j(b'small'), j(b'hard link'))
    open(j(b'empty'), 'wb').close()
    with open(j(b'nl\nname'), 'wb') as f:
        f.write(b'newline')
    with open(j(b'back\\slash \\0A'), 'wb') as f:
        f.write(b'backslash')
    with open(j('ümlaut €'.encode()), 'wb') as f:
        f.write(b'utf8')
    with open(j(b'latin1-\xe4'), 'wb') as f:
        f.write(b'latin1')
    os.symlink(b'd/text.txt', j(b'link'))
    os.symlink(b'/nonexistent/target', j(b'd', b'dangling'))
    os.mkfifo(j(b'd', b'fifo'))
    with open(j(b'd', b'secret'), 'wb') as f:
        f.write(b'only root')
    os.chmod(j(b'd', b'secret'), 0o600)
    os.chown(j(b'd', b'secret'), 1234, 5678)
    os.chmod(j(b'd', b'sub'), 0o1777)
    os.utime(j(b'small'), (1600000000, 1600000000))
    os.utime(j(b'd', b'sub'), (1500000000, 1500000000))


def run_storebackup(tmp, src, series='s', extra=''):
    conf = os.path.join(tmp, 'storeBackup.conf')
    with open(conf, 'w') as f:
        f.write(extra + f"""sourceDir={src}
backupDir={tmp}/backups
series={series}
lockFile={tmp}/lock
logFile={tmp}/log
checkBlocksRule0='$size > 250000' or '$file =~ /\\.img\\Z/'
checkBlocksBS0=64k
checkBlocksCompr0=check
""")
    os.makedirs(os.path.join(tmp, 'backups'), exist_ok=True)
    before = set(os.listdir(os.path.join(tmp, 'backups', series))) \
        if os.path.isdir(os.path.join(tmp, 'backups', series)) else set()
    subprocess.run([STOREBACKUP, '-f', conf], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    new = sorted(set(os.listdir(os.path.join(tmp, 'backups', series)))
                 - before - {'last'})
    assert len(new) == 1, new
    return os.path.join(tmp, 'backups', series, new[0])


@pytest.fixture(scope='session')
def storebackup_pair(tmp_path_factory):
    """Two consecutive storeBackup backups of the same source; between them
    d/text.txt is changed and everything else stays the same."""
    if STOREBACKUP is None:
        pytest.skip('storeBackup.pl not installed')
    if os.geteuid() != 0:
        pytest.skip('storeBackup must run as root to keep owners')
    tmp = str(tmp_path_factory.mktemp('storebackup'))
    src = os.path.join(tmp, 'src')
    make_source(src)
    first = run_storebackup(tmp, src)
    snapshot = os.path.join(tmp, 'src-first')
    subprocess.run(['cp', '-a', src, snapshot], check=True)
    import time
    time.sleep(1.1)             # storeBackup names backups by second
    with open(os.path.join(src, 'd', 'text.txt'), 'ab') as f:
        f.write(b'changed\n')
    second = run_storebackup(tmp, src)
    return {'first': first, 'second': second, 'src_first': snapshot,
            'src': src}


@pytest.fixture(scope='session')
def storebackup_late_links(tmp_path_factory):
    """Backup made with lateLinks and lateCompress (as all series on the
    author's server), before and after storeBackupUpdateBackup.pl."""
    if STOREBACKUP is None:
        pytest.skip('storeBackup.pl not installed')
    if os.geteuid() != 0:
        pytest.skip('storeBackup must run as root to keep owners')
    tmp = str(tmp_path_factory.mktemp('latelinks'))
    src = os.path.join(tmp, 'src')
    make_source(src)
    backup = run_storebackup(tmp, src, extra='lateLinks=yes\n'
                             'lateCompress=yes\n')
    unresolved = os.path.exists(os.path.join(
        backup, '.storeBackupLinks', 'linkFile.bz2'))
    subprocess.run([shutil.which('storeBackupUpdateBackup.pl'), '-b',
                    os.path.join(tmp, 'backups')], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {'backup': backup, 'src': src, 'was_unresolved': unresolved}
