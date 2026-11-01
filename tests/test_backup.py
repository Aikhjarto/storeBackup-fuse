# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Thomas Wagner
import bz2
import concurrent.futures
import gzip
import hashlib
import io
import os
import random
import stat

import pytest

from storebackup_fuse.backup import (
    DIR, FILE, FIFO, SOCKET, SYMLINK, Backup, BackupError, ChecksumError,
    StreamReader, parse_checksum_line, parse_info, parse_version,
    unescape_name)


def read_all(backup, entry, step=65536, verify=True):
    reader = backup.open(entry, verify=verify)
    try:
        out, pos = [], 0
        while True:
            data = reader.read(pos, step)
            if not data:
                return b''.join(out)
            out.append(data)
            pos += len(data)
    finally:
        reader.close()


# -- parsing ---------------------------------------------------------------

def test_unescape_name():
    assert unescape_name(b'a\\0Ab') == b'a\nb'
    assert unescape_name(b'a\\5Cb') == b'a\\b'
    # a literal backslash followed by "0A" is written as \5C0A
    assert unescape_name(b'x\\5C0A') == b'x\\0A'


def test_parse_checksum_line_keeps_spaces_in_names():
    line = (b'0cc175b9c0f1b6a831c399e269772661 u 47-1 2 3 4 5 1 0 0 420 '
            b'dir/a  b c\n')
    fields = parse_checksum_line(line)
    assert fields[0] == b'0cc175b9c0f1b6a831c399e269772661'
    assert fields[11] == b'dir/a  b c'


def test_parse_checksum_line_keeps_leading_white_space():
    line = (b'0cc175b9c0f1b6a831c399e269772661 u 47-1 2 3 4 5 1 0 0 420 '
            b' \tname\n')
    assert parse_checksum_line(line)[11] == b' \tname'


def test_parse_checksum_line_skips_comments_and_garbage():
    assert parse_checksum_line(b'# contents/md5 compr dev-inode\n') is None
    assert parse_checksum_line(b'too few fields\n') is None


def test_parse_version():
    assert parse_version('3.5.2') == (3, 5, 2)
    assert parse_version('3.4.3') == (3, 4, 3) < (3, 4, 3, 1)
    assert parse_version('') == ()


def test_parse_info(tmp_path):
    p = tmp_path / 'info'
    p.write_text("uncompress='bzip2' '-d'\npostfix='.bz2'\nexceptDirs=\n"
                 "comprRule='$size > 1024 and' 'not $file =~ /x/'\n")
    info = parse_info(p)
    assert info['uncompress'] == ['bzip2', '-d']
    assert info['postfix'] == ['.bz2']
    assert info['exceptDirs'] == []
    assert info['comprRule'] == ['$size > 1024 and', 'not $file =~ /x/']


# -- synthetic backups -----------------------------------------------------

def test_tree_types_and_metadata(synthetic):
    sb = synthetic()
    sb.dir(b'etc', mode=0o750, uid=3, gid=4, mtime=1650000000)
    sb.file(b'etc/plain', b'hello', mode=0o640, uid=5, gid=6)
    sb.file(b'etc/packed', b'abc' * 1000, compr='c')
    sb.symlink(b'etc/link', b'plain')
    sb.special('pipe', b'etc/fifo', mode=0o600)
    sb.special('socket', b'etc/sock', mode=0o700)
    b = Backup(sb.write())

    etc = b.lookup('etc')
    assert etc.kind == DIR and etc.mode == 0o750 and etc.uid == 3
    assert etc.mtime == 1650000000
    assert stat.S_ISDIR(etc.st_mode)
    plain = b.lookup('etc/plain')
    assert (plain.kind, plain.size, plain.uid, plain.gid) == (FILE, 5, 5, 6)
    assert stat.S_IMODE(plain.st_mode) == 0o640
    assert read_all(b, plain) == b'hello'
    assert read_all(b, b.lookup('etc/packed')) == b'abc' * 1000
    assert b.readlink(b.lookup('etc/link')) == b'plain'
    assert b.lookup('etc/fifo').kind == FIFO
    assert stat.S_ISFIFO(b.lookup('etc/fifo').st_mode)
    assert b.lookup('etc/sock').kind == SOCKET
    assert b.counts == {'dir': 1, 'file': 2, 'symlink': 1, 'pipe': 1,
                        'socket': 1}
    with pytest.raises(KeyError):
        b.lookup('etc/missing')


def test_special_names(synthetic):
    sb = synthetic()
    sb.file(b'new\nline', b'1')
    sb.file(b'back\\slash', b'2')
    sb.file(b'with space', b'3')
    sb.file(b'latin1-\xe4', b'4')
    sb.file(b' lead', b'5')
    sb.file(b'\ttab', b'6')
    sb.blocked(b' big.img', b'7' * 1000, block_size=256)
    b = Backup(sb.write())
    assert sorted(b.root.children) == [b'\ttab', b' big.img', b' lead',
                                       b'back\\slash', b'latin1-\xe4',
                                       b'new\nline', b'with space']
    assert read_all(b, b.lookup(b'new\nline')) == b'1'
    assert read_all(b, b.lookup(b' lead')) == b'5'
    assert read_all(b, b.lookup(b'\ttab')) == b'6'
    assert read_all(b, b.lookup(b' big.img')) == b'7' * 1000


@pytest.mark.parametrize('name', [b'../x', b'a/../x', b'a/./x', b'a//x'])
def test_names_leaving_the_backup_refused(synthetic, name):
    sb = synthetic()
    sb.raw_line(b'0cc175b9c0f1b6a831c399e269772661 u 1-1 0 1 1 1 1 0 0 '
                b'420 ' + name + b'\n')
    with pytest.raises(BackupError, match='not a plain relative path'):
        Backup(sb.write())


@pytest.mark.parametrize('block', [b'/etc/passwd', b'../../etc/passwd'])
def test_block_paths_leaving_the_backup_refused(synthetic, block):
    sb = synthetic()
    sb.blocked(b'disk.img', b'data', block_size=65536)
    with bz2.open(os.path.join(sb.path, 'disk.img',
                               '.md5BlockCheckSums.bz2'), 'wb') as f:
        f.write(b'%s u %s\n' % (hashlib.md5(b'data').hexdigest().encode(),
                                block))
    b = Backup(sb.write())
    with pytest.raises(BackupError, match='not a plain relative path'):
        b.open(b.lookup('disk.img'))


def test_malformed_lines_counted(synthetic, caplog):
    sb = synthetic()
    sb.file(b'f', b'x')
    sb.raw_line(b'garbage\n')
    sb.raw_line(b'0cc175b9c0f1b6a831c399e269772661 u 1-1 0 x 1 1 1 0 0 420 '
                b'g\n')
    b = Backup(sb.write())
    assert b.malformed == 2
    assert '2 malformed lines' in caplog.text
    assert list(b.root.children) == [b'f']


def test_implicit_parent_directories(synthetic):
    sb = synthetic()
    sb.file(b'usr/local/bin/tool', b'#!/bin/sh\n', mode=0o755)
    b = Backup(sb.write())
    usr = b.lookup('usr')
    assert usr.kind == DIR and usr.mode == 0o755
    assert usr.mtime == int(os.stat(sb.path).st_mtime)
    assert b.lookup('usr/local/bin').nlink == 2
    assert b.lookup('usr').nlink == 3
    assert [e.path for e in b.walk()] == [b'usr', b'usr/local',
                                          b'usr/local/bin',
                                          b'usr/local/bin/tool']


def test_walk_order_and_parents(synthetic):
    sb = synthetic()
    sb.dir(b'b')
    sb.file(b'b/y', b'')
    sb.file(b'a', b'')
    b = Backup(sb.write())
    assert [e.path for e in b.walk()] == [b'a', b'b', b'b/y']
    assert b.lookup('b/y').parent is b.lookup('b')


def test_blocked_file_compressed_and_random_access(synthetic):
    data = bytes(range(256)) * 1000 + b'end'          # 256003 bytes
    sb = synthetic()
    sb.blocked(b'disk.img', data, block_size=65536)
    b = Backup(sb.write())
    entry = b.lookup('disk.img')
    assert entry.size == len(data)
    assert read_all(b, entry, step=100000) == data
    reader = b.open(entry, verify=False)
    assert reader.read(65530, 20) == data[65530:65550]   # across a block
    assert reader.read(10, 5) == data[10:15]              # backwards
    assert reader.read(len(data) - 3, 100) == b'end'
    assert reader.read(len(data), 100) == b''
    reader.close()


def test_blocked_file_uncompressed_blocks(synthetic):
    data = os.urandom(150000)
    sb = synthetic()
    sb.blocked(b'raw.bin', data, block_size=65536, compress=False)
    b = Backup(sb.write())
    assert read_all(b, b.lookup('raw.bin'), step=4096) == data


@pytest.mark.parametrize('compress', [True, False])
def test_blocked_file_damaged_block(synthetic, compress):
    """Each block is checked when it is loaded, whatever the read order."""
    data = os.urandom(5 * 65536)
    sb = synthetic()
    blocks = sb.blocked(b'disk.img', data, block_size=65536,
                        compress=compress)
    b = Backup(sb.write())
    with open(blocks[3], 'wb') as f:
        bad = bytearray(data[3 * 65536:4 * 65536])
        bad[100] ^= 1
        f.write(bz2.compress(bad) if compress else bad)
    entry = b.lookup('disk.img')
    reader = b.open(entry)
    assert reader.read(0, 100) == data[:100]
    with pytest.raises(ChecksumError, match='does not match'):
        reader.read(3 * 65536 + 10, 100)          # skipped blocks 1 and 2
    reader.close()
    assert entry.inode in b.bad
    reader = b.open(entry, verify=False)
    assert reader.read(3 * 65536 + 100, 1) != data[3 * 65536 + 100:][:1]
    reader.close()


def test_blocked_file_with_wrong_md5(synthetic):
    data = os.urandom(3 * 65536)
    sb = synthetic()
    sb.blocked(b'disk.img', data, block_size=65536, md5='0' * 32)
    b = Backup(sb.write())
    with pytest.raises(ChecksumError, match='do not match'):
        read_all(b, b.lookup('disk.img'))


@pytest.mark.parametrize('stored', [3 * 65536 + 500, 2 * 65536 + 500])
def test_blocked_file_changed_during_backup(synthetic, caplog, stored):
    """storeBackup records the size from before reading a file in blocks
    and the MD5 of what it read; such a file is shown with the recorded
    size, not reported as damaged."""
    data = os.urandom(stored)
    recorded = 3 * 65536 - 1000
    sb = synthetic()
    sb.blocked(b'grew.img', data, block_size=65536, size=recorded)
    b = Backup(sb.write())
    entry = b.lookup('grew.img')
    assert entry.size == recorded
    assert read_all(b, entry) == data[:recorded]
    assert 'changed while storeBackup read it' in caplog.text
    assert entry.inode not in b.bad


def test_blocked_file_block_of_wrong_length(synthetic):
    data = os.urandom(3 * 65536)
    sb = synthetic()
    blocks = sb.blocked(b'disk.img', data, block_size=65536,
                        compress=False)
    b = Backup(sb.write())
    with open(blocks[1], 'ab') as f:
        f.write(b'x')
    reader = b.open(b.lookup('disk.img'), verify=False)
    with pytest.raises(BackupError, match='65537 bytes instead of 65536'):
        reader.read(65536, 10)
    reader.close()


@pytest.mark.parametrize('name', [b'disk.img', b'c.txt'])
def test_parallel_reads_on_one_reader(synthetic, name):
    data = os.urandom(20 * 16384)
    sb = synthetic()
    sb.blocked(b'disk.img', data, block_size=16384)
    sb.file(b'c.txt', data, compr='c')
    b = Backup(sb.write())
    reader = b.open(b.lookup(name), verify=False)
    offsets = [random.Random(i).randrange(len(data)) for i in range(400)]
    with concurrent.futures.ThreadPoolExecutor(8) as pool:
        results = pool.map(lambda o: (o, reader.read(o, 5000)), offsets)
        for off, got in results:
            assert got == data[off:off + 5000], off
    reader.close()


def test_external_uncompress(synthetic):
    """An uncompress= program without a built-in equivalent, e.g. zstd."""
    data = os.urandom(3 * 65536 + 7)
    sb = synthetic(uncompress="'sh' '-c' 'bzip2 -d'")
    sb.blocked(b'disk.img', data, block_size=65536)
    sb.file(b'c.txt', data, compr='c')
    b = Backup(sb.write())
    assert read_all(b, b.lookup('disk.img')) == data
    assert read_all(b, b.lookup('c.txt')) == data


def test_external_uncompress_failing(synthetic):
    sb = synthetic(uncompress="'sh' '-c' 'cat >/dev/null; exit 3'")
    sb.blocked(b'disk.img', b'data', block_size=65536)
    sb.file(b'c.txt', b'data', compr='c')
    b = Backup(sb.write())
    for name in ('disk.img', 'c.txt'):
        with pytest.raises(BackupError, match='status 3'):
            read_all(b, b.lookup(name), verify=False)


def test_damaged_gzip_file(synthetic):
    sb = synthetic(uncompress="'gzip' '-d'")
    sb.file(b'c.txt', b'x' * 100000, compr='c', compress=gzip.compress)
    b = Backup(sb.write())
    assert read_all(b, b.lookup('c.txt')) == b'x' * 100000
    path = os.path.join(sb.path, 'c.txt.bz2')
    with open(path, 'r+b') as f:
        f.seek(20)
        f.write(b'\xff' * 20)
    with pytest.raises(Exception) as e:
        read_all(b, b.lookup('c.txt'), verify=False)
    assert not isinstance(e.value, AssertionError)


def test_stream_reader_restarts_for_backward_reads():
    opened = []

    def opener():
        opened.append(1)
        return io.BytesIO(b'0123456789')
    r = StreamReader(opener, 10)
    assert r.read(2, 3) == b'234'
    assert r.read(5, 2) == b'56'
    assert len(opened) == 1
    assert r.read(0, 2) == b'01'
    assert len(opened) == 2
    assert r.read(20, 2) == b''


def test_stream_reader_detects_early_end():
    r = StreamReader(lambda: io.BytesIO(b'0123'), 10)
    assert r.read(0, 4) == b'0123'
    with pytest.raises(ChecksumError, match='ends after 4 bytes'):
        r.read(4, 4)


def test_verify_detects_corruption(synthetic):
    sb = synthetic()
    sb.file(b'f', b'original', compr='c')
    sb.file(b'g', b'plain data')
    b = Backup(sb.write())
    with open(os.path.join(sb.path, 'g'), 'wb') as f:
        f.write(b'PLAIN DATA')                         # same size
    with pytest.raises(ChecksumError):
        read_all(b, b.lookup('g'))
    assert read_all(b, b.lookup('g'), verify=False) == b'PLAIN DATA'
    assert read_all(b, b.lookup('f')) == b'original'


def test_verify_detects_wrong_size(synthetic):
    sb = synthetic()
    sb.file(b'f', b'12345')
    b = Backup(sb.write())
    with open(os.path.join(sb.path, 'f'), 'wb') as f:
        f.write(b'123')
    with pytest.raises(ChecksumError):
        read_all(b, b.lookup('f'))


def test_verify_out_of_order_reads(synthetic):
    """The kernel sends parallel reads, which may arrive in any order."""
    data = os.urandom(100000)
    sb = synthetic()
    sb.file(b'good', data)
    sb.file(b'bad', data)
    b = Backup(sb.write())
    with open(os.path.join(sb.path, 'bad'), 'r+b') as f:
        f.seek(50000)
        f.write(b'X')
    offsets = list(range(0, len(data), 4096))
    random.Random(1).shuffle(offsets)
    for name in ('good', 'bad'):
        reader = b.open(b.lookup(name))
        try:
            for off in offsets[:-1]:
                reader.read(off, 4096)
                reader.read(off, 100)           # repeated read
            if name == 'bad':
                with pytest.raises(ChecksumError):
                    reader.read(offsets[-1], 4096)
            else:
                reader.read(offsets[-1], 4096)
        finally:
            reader.close()
    assert b.lookup('good').inode in b.verified
    assert b.lookup('bad').inode in b.bad


def test_verify_gives_up_on_scattered_reads(synthetic, monkeypatch):
    from storebackup_fuse.backup import VerifyingReader
    monkeypatch.setattr(VerifyingReader, 'MAX_AHEAD', 10)
    sb = synthetic()
    sb.file(b'f', b'abcdefghijklmnopqrstuvwxyz')
    b = Backup(sb.write())
    with open(os.path.join(sb.path, 'f'), 'wb') as f:
        f.write(b'ABCDEFGHIJKLMNOPQRSTUVWXYZ')
    reader = b.open(b.lookup('f'))
    assert reader.read(10, 8) == b'KLMNOPQR'
    assert reader.read(20, 6) == b'UVWXYZ'      # beyond MAX_AHEAD: no check
    assert reader.read(0, 26) == b'ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    reader.close()
    assert not b.bad and not b.verified


def test_damaged_file_cannot_be_opened_again(synthetic):
    sb = synthetic()
    sb.file(b'a', b'same data', dev_inode='9-9')
    sb.file(b'b', b'same data', dev_inode='9-9')      # hard link of a
    b = Backup(sb.write())
    with open(os.path.join(sb.path, 'a'), 'wb') as f:
        f.write(b'SAME DATA')
    with pytest.raises(ChecksumError):
        read_all(b, b.lookup('a'))
    for name in ('a', 'b'):
        with pytest.raises(ChecksumError, match='do not match'):
            b.open(b.lookup(name))
    assert read_all(b, b.lookup('b'), verify=False) == b'same data'


def test_stored_file_of_wrong_size(synthetic):
    sb = synthetic()
    sb.file(b'f', b'12345')
    b = Backup(sb.write())
    with open(os.path.join(sb.path, 'f'), 'wb') as f:
        f.write(b'123')
    with pytest.raises(ChecksumError, match='3 bytes'):
        b.open(b.lookup('f'), verify=False)


def test_empty_file(synthetic):
    sb = synthetic()
    sb.file(b'empty', b'')
    b = Backup(sb.write())
    assert read_all(b, b.lookup('empty')) == b''


def test_hard_links_share_inode(synthetic):
    sb = synthetic()
    sb.file(b'a', b'same', dev_inode='9-9')
    sb.file(b'b', b'same', dev_inode='9-9')       # hard link in the source
    sb.file(b'c', b'same', dev_inode='9-10')      # same content, other file
    b = Backup(sb.write())
    a, bb, c = (b.lookup(n) for n in 'abc')
    assert a.inode == bb.inode != c.inode
    assert (a.nlink, bb.nlink, c.nlink) == (2, 2, 1)


def test_inodes_independent_of_source_inodes(synthetic):
    """Sources read over sshfs get new device and inode numbers with every
    mount; unchanged files must keep their inode number anyway."""
    def make(name, dev):
        sb = synthetic(name)
        sb.dir(b'd', dev_inode=f'{dev}-1')
        sb.file(b'd/f', b'content', dev_inode=f'{dev}-2')
        sb.file(b'd/g', b'content', dev_inode=f'{dev}-3')
        sb.file(b'h1', b'linked', dev_inode=f'{dev}-4')
        sb.file(b'h2', b'linked', dev_inode=f'{dev}-4')
        sb.symlink(b'd/l', b'f', dev_inode=f'{dev}-5')
        sb.special('pipe', b'd/p', dev_inode=f'{dev}-6')
        return Backup(sb.write())
    one = make('2026.01.01_00.00.00', 53)
    two = make('2026.01.02_00.00.00', 75)
    for path in ('d', 'd/f', 'd/g', 'h1', 'h2', 'd/l', 'd/p'):
        assert one.lookup(path).inode == two.lookup(path).inode, path
    # same contents at another path is another file
    assert one.lookup('d/f').inode != one.lookup('d/g').inode
    assert one.lookup('h1').inode == one.lookup('h2').inode
    assert one.lookup('h1').nlink == 2


def test_reused_source_inode_is_no_hard_link(synthetic):
    """Two files that got the same source inode one after the other during
    the backup (e.g. empty temporary files) are not merged."""
    sb = synthetic()
    sb.file(b'a', b'', dev_inode='9-9', uid=1, mode=0o600)
    sb.file(b'b', b'', dev_inode='9-9', uid=2, mode=0o644)
    sb.file(b'c', b'', dev_inode='9-9', uid=1, mode=0o600, mtime=1)
    b = Backup(sb.write())
    a, bb, c = (b.lookup(n) for n in 'abc')
    assert len({a.inode, bb.inode, c.inode}) == 3
    assert (a.nlink, bb.nlink, c.nlink) == (1, 1, 1)
    assert b.by_inode[bb.inode] is bb


def test_inodes_are_unique_and_stable(synthetic):
    def make(name):
        sb = synthetic(name)
        sb.dir(b'd')
        sb.file(b'd/f', b'content', dev_inode='5-5')
        sb.symlink(b'd/l', b'f', dev_inode='5-6')
        return Backup(sb.write())
    one, two = make('2026.01.01_00.00.00'), make('2026.01.02_00.00.00')
    for path in ('d', 'd/f', 'd/l'):
        assert one.lookup(path).inode == two.lookup(path).inode
    inodes = [e.inode for e in one.walk()]
    assert len(set(inodes)) == len(inodes)
    assert all(i > 1 for i in inodes)


def test_not_a_backup(tmp_path):
    with pytest.raises(BackupError, match='not a storeBackup backup'):
        Backup(str(tmp_path))


def test_unfinished_backup_refused(synthetic):
    sb = synthetic(finished=False)
    sb.file(b'f', b'x')
    path = sb.write()
    with pytest.raises(BackupError, match='not finished'):
        Backup(path)
    assert Backup(path, allow_unfinished=True).lookup('f').size == 1


@pytest.mark.parametrize('version', [None, '3.2', '3.4.3'])
def test_old_backups_finished_without_flag(synthetic, version):
    """Up to storeBackup 3.4.3, a backup is unfinished while
    .md5CheckSums.notFinished exists (checkIfBackupWasFinished)."""
    sb = synthetic(finished=False, version=version)
    sb.file(b'f', b'x')
    path = sb.write()
    assert Backup(path).lookup('f').size == 1
    open(os.path.join(path, '.md5CheckSums.notFinished'), 'w').close()
    with pytest.raises(BackupError, match='not finished'):
        Backup(path)


def test_unresolved_late_links_refused(synthetic):
    sb = synthetic()
    sb.file(b'f', b'x')
    path = sb.write()
    open(os.path.join(path, '.storeBackupLinks', 'linkFile.bz2'),
         'w').close()
    with pytest.raises(BackupError, match='unresolved lateLinks'):
        Backup(path)


def test_unknown_entry_type(synthetic):
    sb = synthetic()
    sb.raw_line(b'weird u 1-1 0 1 1 1 0 0 0 420 f\n')
    with pytest.raises(BackupError, match='unknown entry type'):
        Backup(sb.write())


def test_file_listed_as_parent(synthetic):
    sb = synthetic()
    sb.file(b'f', b'x')
    sb.raw_line(b'dir 0 1-1 0 1 1 1 0 0 0 493 f/sub\n')
    with pytest.raises(BackupError, match='parent directory'):
        Backup(sb.write())


# -- backups made by storeBackup.pl ----------------------------------------

def compare_with_source(backup, src):
    """Every source path must be in the backup with the same type, owner,
    mode, mtime and contents, and nothing else."""
    src = os.fsencode(src)
    seen = set()
    for dirpath, dirnames, filenames in os.walk(src):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, src)
            seen.add(rel)
            st = os.lstat(full)
            entry = backup.lookup(rel)
            assert entry.st_mode == st.st_mode, rel
            assert (entry.uid, entry.gid) == (st.st_uid, st.st_gid), rel
            if not stat.S_ISLNK(st.st_mode):
                assert entry.mtime == int(st.st_mtime), rel
            if stat.S_ISREG(st.st_mode):
                with open(full, 'rb') as f:
                    assert read_all(backup, entry) == f.read(), rel
                assert entry.nlink == st.st_nlink, rel
            elif stat.S_ISLNK(st.st_mode):
                assert backup.readlink(entry) == os.readlink(full), rel
    assert {e.path for e in backup.walk()} == seen


def test_storebackup_backup_matches_source(storebackup_pair):
    backup = Backup(storebackup_pair['first'])
    compare_with_source(backup, storebackup_pair['src_first'])
    # the cases this test is meant to cover are really in the backup
    assert backup.lookup('random.bin').compr == 'b'
    assert backup.lookup('zeros.img').compr == 'b'
    assert backup.lookup('d/text.txt').compr == 'c'
    assert backup.lookup('small').inode == backup.lookup('hard link').inode


def test_storebackup_second_backup(storebackup_pair):
    first = Backup(storebackup_pair['first'])
    second = Backup(storebackup_pair['second'])
    compare_with_source(second, storebackup_pair['src'])
    for path in ('random.bin', 'zeros.img', 'small', 'link', 'd', 'd/fifo'):
        assert first.lookup(path).inode == second.lookup(path).inode, path
    assert first.lookup('d/text.txt').inode != \
        second.lookup('d/text.txt').inode


def test_storebackup_md5_matches(storebackup_pair):
    backup = Backup(storebackup_pair['first'])
    for entry in backup.walk():
        if entry.kind == FILE:
            data = read_all(backup, entry, verify=False)
            assert hashlib.md5(data).hexdigest() == entry.md5, entry.path
        elif entry.kind == SYMLINK:
            assert backup.readlink(entry)
