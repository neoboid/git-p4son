"""
Sync-split command implementation for git-p4son.

sync-split has been folded into sync, which splits out the configured split
users' changelists (see sync_split_users). The command is kept only to tell
anyone still running it what to run instead.
"""

import argparse
import shlex

from .log import log
from .sync_split_users import USER_PLACEHOLDER, current_p4_user


def sync_split_command(args: argparse.Namespace) -> int:
    """Explain how to get what sync-split did with sync, without syncing."""
    # sync-split split out the current user by default; name them if p4
    # can tell, or fall back on the placeholder sync resolves itself.
    users = args.user or [current_p4_user(args.workspace_dir)
                          or USER_PLACEHOLDER]
    sync = ['git', 'p4son', 'sync']
    for user in users:
        sync += ['-u', user]
    if args.dry_run:
        sync.append('--dry-run')
    if args.changelist:
        sync.append(args.changelist)

    add = ['git', 'p4son', 'sync-split-users', 'add']
    add += args.user or ['--me']

    log.error('sync-split has been folded into sync.')
    log.info('Run instead:')
    log.info(f'  {shlex.join(sync)}')
    log.info('To split them out on every sync:')
    log.info(f'  {shlex.join(add)}')
    return 1
