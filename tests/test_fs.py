# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Thomas Wagner
"""Mount backups with the real FUSE module and look at them through the
kernel.  Skipped where FUSE cannot be used."""
import errno
import os
import shutil
import stat
import subprocess
import sys
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

    def do(backup, *args):
        m = Mount(backup, tmp_path / f'mnt{len(mounts)}', *args)
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


def test_permissions_enforced_for_other_users(synthetic, mount):
    if os.geteuid() != 0:
        pytest.skip('needs root to switch users')
    sb = synthetic()
    path = os.path.dirname(os.path.abspath(sb.path))
    while path != '/':
        if not os.stat(path).st_mode & stat.S_IXOTH:
            pytest.skip(f'{path} is not accessible for other users')
        path = os.path.dirname(path)
    sb.file(b'secret', b'x', mode=0o600, uid=0, gid=0)
    sb.file(b'public', b'y', mode=0o644, uid=0, gid=0)
    m = mount(sb.write(), '--allow-other')
    os.chmod(os.path.dirname(m.mountpoint), 0o755)
    run = lambda p: subprocess.run(
        ['setpriv', '--reuid=65534', '--regid=65534', '--clear-groups',
         'cat', os.path.join(m.mountpoint, p)], capture_output=True)
    assert run('public').stdout == b'y'
    assert run('secret').returncode != 0
