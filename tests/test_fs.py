# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Thomas Wagner
"""Mount backups with the real FUSE module and look at them through the
kernel.  Skipped where FUSE cannot be used."""
import bz2
import concurrent.futures
import errno
import gzip
import hashlib
import os
import random
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time

import pytest

try:
    import pyfuse3  # noqa: F401
    HAVE_PYFUSE3 = True
except ImportError:
    HAVE_PYFUSE3 = False

pytestmark = pytest.mark.skipif(
    not (HAVE_PYFUSE3 and os.path.exists('/dev/fuse')
         and shutil.which('fusermount3')),
    reason='FUSE not available')

SRC = os.path.join(os.path.dirname(__file__), '..', 'src')


class Mount:
    def __init__(self, backup, mountpoint, *args):
        self.mountpoint = str(mountpoint)
        os.makedirs(self.mountpoint, exist_ok=True)
        env = dict(os.environ, PYTHONPATH=SRC)
        self.proc = subprocess.Popen(
            [sys.executable, '-m', 'storebackup_fuse', *args, backup,
             self.mountpoint],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(100):
            if os.path.ismount(self.mountpoint):
                return
            if self.proc.poll() is not None:
                raise RuntimeError(self.proc.stdout.read().decode())
            time.sleep(0.1)
        self.unmount()
        raise RuntimeError('mount did not appear')

    def unmount(self):
        subprocess.run(['fusermount3', '-u', self.mountpoint], check=False)
        try:
            out, _ = self.proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            out, _ = self.proc.communicate()
        self.log = out.decode()
        return self.proc.returncode


@pytest.fixture
def mount(tmp_path):
    mounts = []

    def do(backup, *args, mountpoint=None):
        m = Mount(backup, mountpoint or tmp_path / f'mnt{len(mounts)}',
                  *args)
        mounts.append(m)
        return m
    yield do
    for m in mounts:
        if os.path.ismount(m.mountpoint):
            m.unmount()


def compare_trees(src, mnt):
    src, mnt = os.fsencode(src), os.fsencode(mnt)
    for dirpath, dirnames, filenames in os.walk(src):
        rel_dir = os.path.relpath(dirpath, src)
        assert sorted(os.listdir(os.path.join(mnt, rel_dir))) == \
            sorted(dirnames + filenames), rel_dir
        for name in dirnames + filenames:
            rel = os.path.normpath(os.path.join(rel_dir, name))
            a = os.lstat(os.path.join(src, rel))
            b = os.lstat(os.path.join(mnt, rel))
            assert a.st_mode == b.st_mode, rel
            assert (a.st_uid, a.st_gid) == (b.st_uid, b.st_gid), rel
            if not stat.S_ISLNK(a.st_mode):
                assert int(a.st_mtime) == b.st_mtime, rel
            if stat.S_ISREG(a.st_mode):
                assert a.st_size == b.st_size, rel
                assert a.st_nlink == b.st_nlink, rel
                with open(os.path.join(src, rel), 'rb') as f1, \
                        open(os.path.join(mnt, rel), 'rb') as f2:
                    assert f1.read() == f2.read(), rel
            elif stat.S_ISLNK(a.st_mode):
                assert os.readlink(os.path.join(src, rel)) == \
                    os.readlink(os.path.join(mnt, rel)), rel


def test_mount_storebackup_backup(storebackup_pair, mount):
    m = mount(storebackup_pair['first'])
    compare_trees(storebackup_pair['src_first'], m.mountpoint)
    a = os.lstat(os.path.join(m.mountpoint, 'small'))
    b = os.lstat(os.path.join(m.mountpoint, 'hard link'))
    assert a.st_ino == b.st_ino
    assert m.unmount() == 0, m.log


def test_inodes_stable_between_backups(storebackup_pair, mount):
    m1 = mount(storebackup_pair['first'])
    m2 = mount(storebackup_pair['second'])
    ino = lambda m, p: os.lstat(os.path.join(m.mountpoint, p)).st_ino
    assert ino(m1, 'random.bin') == ino(m2, 'random.bin')
    assert ino(m1, 'd/text.txt') != ino(m2, 'd/text.txt')


def test_read_only(synthetic, mount):
    sb = synthetic()
    sb.file(b'f', b'data')
    m = mount(sb.write())
    with pytest.raises(OSError) as e:
        open(os.path.join(m.mountpoint, 'f'), 'r+b')
    assert e.value.errno in (errno.EROFS, errno.EACCES)
    with pytest.raises(OSError):
        os.mkdir(os.path.join(m.mountpoint, 'new'))


def test_corrupted_file_gives_io_error(synthetic, mount):
    sb = synthetic()
    sb.file(b'bad', b'good data')
    path = sb.write()
    with open(os.path.join(path, 'bad'), 'wb') as f:
        f.write(b'evil data')
    m = mount(path)
    with pytest.raises(OSError) as e:
        with open(os.path.join(m.mountpoint, 'bad'), 'rb') as f:
            f.read()
    assert e.value.errno == errno.EIO
    m.unmount()
    assert 'do not match the checksum file' in m.log


@pytest.fixture
def public_dir():
    """Directory other users can reach (pytest's own are mode 0700)."""
    path = tempfile.mkdtemp(prefix='storebackup-fuse-test-')
    os.chmod(path, 0o755)
    parent = os.path.dirname(path)
    while parent != '/':
        if not os.stat(parent).st_mode & stat.S_IXOTH:
            os.rmdir(path)
            pytest.skip(f'{parent} is not accessible for other users')
        parent = os.path.dirname(parent)
    yield path
    shutil.rmtree(path)


def test_permissions_enforced_for_other_users(synthetic, mount, public_dir):
    if os.geteuid() != 0:
        pytest.skip('needs root to switch users')
    sb = synthetic()
    sb.file(b'secret', b'x', mode=0o600, uid=0, gid=0)
    sb.file(b'public', b'y', mode=0o644, uid=0, gid=0)
    m = mount(sb.write(), '--allow-other',
              mountpoint=os.path.join(public_dir, 'mnt'))
    run = lambda p: subprocess.run(
        ['setpriv', '--reuid=65534', '--regid=65534', '--clear-groups',
         'cat', os.path.join(m.mountpoint, p)], capture_output=True)
    assert run('public').stdout == b'y'
    assert run('secret').returncode != 0
    m.unmount()


def cat(path):
    """Read a file like cat does; returns its MD5 or the errno."""
    try:
        with open(path, 'rb') as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError as e:
        return e.errno


def test_blocked_files_verified_despite_parallel_reads(synthetic, mount):
    """Read-ahead sends reads of one file in parallel and out of order;
    neither a wrong MD5 nor a damaged block may pass."""
    data = os.urandom(8 << 20)
    sb = synthetic()
    for i in range(4):
        sb.blocked(b'wrong-md5-%d.img' % i, data, block_size=256 << 10,
                   md5='0' * 32)
        blocks = sb.blocked(b'bad-block-%d.img' % i, data,
                            block_size=256 << 10)
        with open(blocks[20], 'wb') as f:
            f.write(bz2.compress(b'\0' * (256 << 10)))
    sb.blocked(b'good.img', data, block_size=256 << 10)
    m = mount(sb.write())
    for i in range(4):
        for name in ('wrong-md5-%d.img', 'bad-block-%d.img'):
            assert cat(os.path.join(m.mountpoint, name % i)) == errno.EIO
    assert cat(os.path.join(m.mountpoint, 'good.img')) == \
        hashlib.md5(data).hexdigest()
    assert m.unmount() == 0, m.log


def test_damaged_file_stays_unreadable(synthetic, mount):
    """Neither the page cache nor a hard link may serve the data of a file
    that failed the check, nor of one that was read only in part."""
    data = os.urandom(8 << 20)
    bad = bytearray(data)
    bad[5 << 20] ^= 1
    sb = synthetic()
    sb.file(b'bad', data, dev_inode='7-7')
    sb.file(b'bad link', data, dev_inode='7-7')
    sb.file(b'partly', data)
    path = sb.write()
    for name in ('bad', 'bad link', 'partly'):
        with open(os.path.join(path, name), 'wb') as f:
            f.write(bad)
    m = mount(path)
    j = lambda p: os.path.join(m.mountpoint, p)
    assert cat(j('bad')) == errno.EIO
    assert cat(j('bad')) == errno.EIO
    assert cat(j('bad link')) == errno.EIO
    with open(j('partly'), 'rb') as f:
        f.read(4096)
    assert cat(j('partly')) == errno.EIO
    m.unmount()
    assert m.proc.returncode == 0, m.log


def test_errors_do_not_end_the_mount(synthetic, mount):
    sb = synthetic(uncompress="'gzip' '-d'")
    sb.file(b'damaged.gz.txt', b'x' * 100000, compr='c',
            compress=gzip.compress)
    sb.raw_line(b'symlink 0 1-1 0 1 1 1 0 0 0 0 missing-link\n')
    sb.file(b'ok', b'fine')
    path = sb.write()
    with open(os.path.join(path, 'damaged.gz.txt.bz2'), 'r+b') as f:
        f.seek(20)
        f.write(b'\xff' * 20)
    m = mount(path)
    j = lambda p: os.path.join(m.mountpoint, p)
    assert sorted(os.listdir(m.mountpoint)) == ['damaged.gz.txt',
                                                'missing-link', 'ok']
    assert stat.S_ISLNK(os.lstat(j('missing-link')).st_mode)
    with pytest.raises(OSError) as e:
        os.readlink(j('missing-link'))
    assert e.value.errno == errno.EIO
    assert cat(j('damaged.gz.txt')) == errno.EIO
    assert cat(j('ok')) == hashlib.md5(b'fine').hexdigest()
    assert m.unmount() == 0, m.log


def test_parallel_reads_on_one_descriptor(synthetic, mount):
    data = os.urandom(4 << 20)
    sb = synthetic()
    sb.file(b'c.txt', data, compr='c')
    sb.blocked(b'disk.img', data, block_size=256 << 10)
    m = mount(sb.write(), '--no-verify')
    for name in ('c.txt', 'disk.img'):
        fd = os.open(os.path.join(m.mountpoint, name), os.O_RDONLY |
                     os.O_DIRECT)
        try:
            offsets = [random.Random(i).randrange(len(data) - 65536)
                       for i in range(200)]
            with concurrent.futures.ThreadPoolExecutor(8) as pool:
                for off, got in pool.map(
                        lambda o: (o, os.pread(fd, 65536, o)), offsets):
                    assert got == data[off:off + 65536], (name, off)
        finally:
            os.close(fd)
    assert m.unmount() == 0, m.log


def test_sigterm_unmounts(synthetic, mount):
    sb = synthetic()
    sb.file(b'f', b'x')
    m = mount(sb.write())
    m.proc.send_signal(signal.SIGTERM)
    assert m.proc.wait(timeout=20) == 0
    assert not os.path.ismount(m.mountpoint)
    assert os.listdir(m.mountpoint) == []
