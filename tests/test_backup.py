# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Thomas Wagner
import hashlib
import os
import stat

import pytest

from storebackup_fuse.backup import (
    DIR, FILE, FIFO, SOCKET, SYMLINK, Backup, BackupError, ChecksumError,
    StreamReader, parse_checksum_line, parse_info, unescape_name)


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


def test_parse_checksum_line_skips_comments_and_garbage():
    assert parse_checksum_line(b'# contents/md5 compr dev-inode\n') is None
    assert parse_checksum_line(b'too few fields\n') is None


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
    b = Backup(sb.write())
    assert sorted(b.root.children) == [b'back\\slash', b'latin1-\xe4',
                                       b'new\nline', b'with space']
    assert read_all(b, b.lookup(b'new\nline')) == b'1'


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


def test_stream_reader_restarts_for_backward_reads():
    import io
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


def test_verify_is_skipped_for_out_of_order_reads(synthetic):
    sb = synthetic()
    sb.file(b'f', b'abcdef')
    b = Backup(sb.write())
    with open(os.path.join(sb.path, 'f'), 'wb') as f:
        f.write(b'ABCDEF')
    reader = b.open(b.lookup('f'))
    assert reader.read(3, 3) == b'DEF'                 # not from the start
    assert reader.read(0, 3) == b'ABC'
    assert reader.read(3, 3) == b'DEF'
    reader.close()


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
