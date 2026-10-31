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

The program stays in the foreground until the mountpoint is unmounted;
start it with `&` or as a systemd unit. Without installing:
`PYTHONPATH=src python3 -m storebackup_fuse BACKUP MOUNTPOINT`.

Options:

| Option | Meaning |
|---|---|
| `--no-verify` | Do not check the MD5 sums (see below) |
| `--allow-unfinished` | Also mount backups without storeBackup's finished flag |
| `--allow-other` | Let other users access the mount; access is still checked against the recorded owner and mode |
| `--debug` | Log every FUSE request |

The backup must be complete and its links resolved: backups made with
`lateLinks` need `storeBackupUpdateBackup.pl` first, as for
`storeBackupRecover.pl`.

### Checksums

When a file is read from start to end in order (as backup programs and `cp`
do), its MD5 is compared with the one in `.md5CheckSums`. If they differ, the
read that reaches the end of the file fails with `EIO` and the error is
logged, so damaged files in the backup do not pass unnoticed. Files read out
of order are not checked.

### Inode numbers

Inode numbers are derived from the file's MD5 and the inode it had on the
source, so they are the same in every mount and for every backup in which the
file is unchanged. This lets restic (and other programs that compare inode,
size and times with the previous snapshot) skip unchanged files without
reading them.

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
