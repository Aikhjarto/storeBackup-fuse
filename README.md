# storeBackup-fuse

Read-only FUSE filesystem that shows one backup made by
[storeBackup](https://www.nongnu.org/storebackup/) as plain files:

- files are decompressed on the fly (`.bz2` suffix removed),
- files stored in blocks (`checkBlocks…` options, e.g. VM images) appear as
  one file again,
- owner, group, mode and times are the ones storeBackup recorded in
  `.md5CheckSums`, not those of the stored files (those are hard links shared
  by many backups),
- symlinks, named pipes, sockets and device files appear as such,
- hard links of the source share an inode number.

Nothing is unpacked to disk, so a backup can be read, or handed to another
backup program, without space for a restore. The main use is moving a
storeBackup history to a deduplicating backup program such as
[restic](https://restic.net/), one backup at a time.

## Requirements

- Linux with FUSE 3 (`fusermount3`)
- Python ≥ 3.9 with [pyfuse3](https://github.com/libfuse/pyfuse3) and trio
  (openSUSE: `python3xx-pyfuse3`, Debian: `python3-pyfuse3`)
- Read access to the backup directory (usually root)

storeBackup itself is not needed, except for running the full test suite.

## Usage

```sh
storebackup-fuse /backup/storeBackup/SERIES/2026.10.31_04.09.14 /mnt/sb
# ... use /mnt/sb ...
fusermount3 -u /mnt/sb
```

The program stays in the foreground until the mountpoint is unmounted or it
gets SIGTERM, SIGINT or SIGHUP (then it unmounts itself); start it with `&`
or as a systemd unit. Without installing:
`PYTHONPATH=src python3 -m storebackup_fuse BACKUP MOUNTPOINT`.

Options:

| Option | Meaning |
|---|---|
| `--no-verify` | Do not check the MD5 sums (see below) |
| `--allow-unfinished` | Also mount backups that storeBackup considers unfinished |
| `--allow-external-uncompress` | Run the backup's `uncompress=` program even if its info file is not owned by root (or the caller) or is writable by others |
| `--allow-other` | Let other users access the mount; access is still checked against the recorded owner and mode |
| `--debug` | Log every FUSE request |

The backup must be finished (by the same rule as storeBackup: the flag
`.storeBackupLinks/backup.Finished`, or for backups of storeBackup ≤ 3.4.3 no
`.md5CheckSums.notFinished`) and its links resolved: backups made with
`lateLinks` need `storeBackupUpdateBackup.pl` first, as for
`storeBackupRecover.pl`.

### Checksums

When a file has been read completely (as backup programs and `cp` do, in
any order of the reads), its size and MD5 are compared with
`.md5CheckSums`. Files in blocks are also checked block by block against
their `.md5BlockCheckSums` as the blocks are loaded. On a mismatch, the read
fails with `EIO`, the error is logged, and the file cannot be opened again
during this mount (hard links of it neither); other open handles of it fail
on their next read.

Every handle that reads a file completely gets the error, also when
several programs read the same file (or hard links of it) at the same time:
until a file has been verified, only one handle at a time uses the page
cache and the others read directly (`direct_io`; `mmap` does not work for
them), and data left in the page cache by an incomplete read is dropped.
Files read only in part, or in a very scattered order, are not checked as a
whole. Uncompressed files whose stored size differs from the recorded one
cannot be opened, also with `--no-verify`.

storeBackup records the size of a file in blocks from before reading it but
the MD5 of what it read. If the file changed meanwhile (storeBackup logs
"changed during backup"), it is checked against the MD5 of all stored
blocks (only the blocks at the end are read a second time), shown with
the recorded size (cut off, or filled up with zeros like
`storeBackupRecover.pl` does), and a warning is logged. Unlike
`storeBackupRecover.pl`, which writes all stored blocks, a file that grew is
cut off at the recorded size.

### Decompression

`bzip2`, `gzip` and `xz` (also `lbzip2`, `pigz` etc.) are decompressed with
Python's own modules; files are decompressed by worker threads ahead of the
reads, several files in parallel. Any other `uncompress=` program from
`.md5CheckSums.info` (e.g. `zstd`) is run, with the rights of the caller,
only if the info file belongs to root or the caller, is not writable by
others and is no symbolic link, or with `--allow-external-uncompress`.
Only the file itself is checked, not the directories leading to it: with
an external program, mount only backups below directories that other
users cannot change.

### Inode numbers

Inode numbers are derived from the file's MD5 and path (directories,
symlinks and special files: from the path), so they are the same in every
mount and for every backup in which the file is unchanged. This lets restic
(and other programs that compare inode, size and times with the previous
snapshot) skip unchanged files without reading them. The inode numbers of
the source recorded by storeBackup are not suitable for this: sources read
over sshfs get new ones with every mount.

Hard links of the source share one inode number; they are recognized by the
source inode together with contents, owner, mode and mtime, all within one
backup. Sources read over sshfs have no hard links in the backup, as sshfs
gives every path its own inode number.

## Example: moving a storeBackup series to restic

Mount each backup at the same path, oldest first, and back it up with the
original date:

```sh
series=thomas
mnt=/mnt/sb-$series        # same path for every backup
mkdir -p "$mnt"
for b in /backup/storeBackup/$series/20*; do
    storebackup-fuse "$b" "$mnt" & pid=$!
    until mountpoint -q "$mnt"; do sleep 1; done
    d=$(basename "$b" | sed -E 's/^(....)\.(..)\.(..)_(..)\.(..)\.(..)$/\1-\2-\3 \4:\5:\6/')
    restic backup --host "$series" --tag storebackup --time "$d" "$mnt"
    fusermount3 -u "$mnt"; wait $pid
done
```

restic records the mountpoint as path; `restic restore --target` puts the
files below it, or use `restic restore SNAPSHOT:$mnt`.

Throughput measured on a 4-core machine: listing 800 000 files takes about
20 s, VM images stored as bzip2-compressed blocks are read at about
75 MB/s (blocks are decompressed in parallel ahead of the reader).

## Limitations

- Read-only; extended attributes and ACLs are not shown (storeBackup does not
  record them).
- storeBackup records no mode for symlinks; they are shown as `rwxrwxrwx`,
  as on Linux.
- Special files archived with `specialTypeArchiver` and `archiveTypes` are
  shown with their type, but their archive contents are not interpreted.
- Directories that storeBackup did not list (parents of `includeDirs`) get
  mode 0755, owner root and the date of the backup.
- The whole file list of the mounted backup is kept in memory (about 750 MB
  for 800 000 entries).

## Tests

```sh
python3 -m pytest
```

Tests that run the real `storeBackup.pl` (to compare backups with their
source) are skipped if it is not installed; mount tests are skipped if FUSE is
not available. The full suite needs root.

## License

GPL-2.0-or-later, see [COPYING](COPYING).
