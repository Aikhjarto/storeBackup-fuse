# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Thomas Wagner
"""Command line: storebackup-fuse BACKUP MOUNTPOINT"""

import argparse
import logging
import sys
import time

from . import __version__
from .backup import Backup, BackupError


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog='storebackup-fuse',
        description='Mount one storeBackup backup read-only, showing the '
                    'files uncompressed and with the owner, mode and times '
                    'recorded by storeBackup. Runs in the foreground until '
                    'the mountpoint is unmounted (fusermount3 -u MOUNTPOINT).')
    parser.add_argument('backup', help='backup directory, e.g. '
                        '/backup/storeBackup/SERIES/2026.10.31_04.09.14')
    parser.add_argument('mountpoint')
    parser.add_argument('--no-verify', action='store_true',
                        help='do not check the MD5 of files read in sequence')
    parser.add_argument('--allow-unfinished', action='store_true',
                        help='also mount backups without finished flag')
    parser.add_argument('--allow-other', action='store_true',
                        help='let other users access the mount (needs '
                        'user_allow_other in /etc/fuse.conf for non-root)')
    parser.add_argument('--debug', action='store_true',
                        help='log FUSE requests')
    parser.add_argument('--version', action='version', version=__version__)
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO,
                        format='storebackup-fuse: %(levelname)s %(message)s')
    log = logging.getLogger('storebackup-fuse')

    started = time.monotonic()
    try:
        backup = Backup(args.backup, allow_unfinished=args.allow_unfinished)
    except (OSError, BackupError) as e:
        log.error('%s', e)
        sys.exit(1)
    log.info('%s: %s entries loaded in %.1f s', backup.path,
             ', '.join(f'{n} {k}' for k, n in sorted(backup.counts.items())),
             time.monotonic() - started)

    from .fs import mount           # pyfuse3 only needed for mounting
    try:
        mount(backup, args.mountpoint, verify=not args.no_verify,
              allow_other=args.allow_other, debug=args.debug)
    except KeyboardInterrupt:
        pass
    except RuntimeError as e:
        log.error('%s', e)
        sys.exit(1)


if __name__ == '__main__':
    main()
