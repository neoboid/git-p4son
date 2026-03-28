"""
Sync command implementation for git-p4son.
"""

import argparse
import re
from typing import IO

from .common import CommandError, RunError, run_with_output
from .config import get_depot_root
from .git import add_all_files, commit, get_dirty_files
from .log import log
from .perforce import (
    get_latest_changelist,
    p4_force_sync_file,
    p4_get_opened_files,
)


def git_changelist_of_last_sync(workspace_dir: str) -> int | None:
    """Get the changelist number from the most recent sync commit."""
    res = run_with_output(
        ['git', 'log', '-1', '--pretty=%s',
         '--grep=: p4 sync //'],
        cwd=workspace_dir)
    if len(res.stdout) == 0:
        return None

    msg = res.stdout[0]
    pattern = r"^(\d+|pergit|git-p4son): p4 sync //.+@(\d+)$"
    match = re.search(pattern, msg)
    if match:
        return int(match.group(2))
    else:
        return None


def get_writable_files(stderr_lines: list[str]) -> list[str]:
    """Extract writable files from p4 sync stderr output."""
    prefix = "Can't clobber writable file "
    return [line[len(prefix):].rstrip()
            for line in stderr_lines if line.startswith(prefix)]


def parse_p4_sync_line(line: str) -> tuple[str | None, str | None]:
    """Parse a line from p4 sync output."""
    patterns = [
        ('add', ' - added as '),
        ('del', ' - deleted as '),
        ('upd', ' - updating '),
        ('clb', "Can't clobber writable file ")
    ]
    for mode, pattern in patterns:
        tokens = line.split(pattern)
        if len(tokens) == 2:
            return (mode, tokens[1])

    return (None, None)


class P4SyncOutputProcessor:
    """Process p4 sync output in real-time."""

    def __init__(self) -> None:
        self.synced_file_count: int = 0
        self.stats: dict[str, int] = {
            mode: 0 for mode in ['add', 'del', 'upd', 'clb']}

    def __call__(self, line: str, stream: IO[str]) -> None:
        if re.search(r"@\d+ - file\(s\) up-to-date\.", line):
            log.info('all files up to date')
            return

        mode, filename = parse_p4_sync_line(line)
        if not mode or not filename:
            log.warning(f'Unparsable line: {line}')
            return

        self.stats[mode] += 1
        self.synced_file_count += 1

    def get_summary(self) -> str:
        """Get a one-line sync summary."""
        synced_count = self.stats['add'] + \
            self.stats['upd'] - self.stats['clb']
        parts = []
        if self.stats['add']:
            parts.append(f"add: {self.stats['add']}")
        if self.stats['upd']:
            parts.append(f"upd: {self.stats['upd']}")
        if self.stats['del']:
            parts.append(f"del: {self.stats['del']}")
        if self.stats['clb']:
            parts.append(f"clb: {self.stats['clb']}")
        detail = ', '.join(parts)
        if detail:
            return f'synced {synced_count} files ({detail})'
        return f'synced {synced_count} files'


def p4_sync(changelist: int, label: str, force: bool, depot_root: str,
            workspace_dir: str) -> bool:
    """Sync files from Perforce.

    Returns True on success, False if writable files were found without --force.
    Raises CommandError on actual command failures.
    """
    log.heading(f'Syncing to {label} CL ({changelist})')

    output_processor = P4SyncOutputProcessor()
    try:
        result = run_with_output(
            ['p4', 'sync', f'{depot_root}/...@{changelist}'],
            cwd=workspace_dir, on_output=output_processor)
        if result.elapsed:
            log.elapsed(result.elapsed)
        log.success(output_processor.get_summary())
        return True
    except RunError as e:
        log.info(output_processor.get_summary())
        writable_files = get_writable_files(e.stderr)
        if not writable_files:
            raise
        if force:
            for filename in writable_files:
                p4_force_sync_file(changelist, filename, workspace_dir)
            log.success(f'Force synced {len(writable_files)} writable files')
            return True
        else:
            log.info('Leaving files as is, use --force to force sync')
            for filename in writable_files:
                log.info(filename)
            log.error('Failed to sync files from perforce')
            return False


def sync_command(args: argparse.Namespace) -> int:
    """
    Execute the sync command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = args.workspace_dir

    log.heading('Finding depot root')
    depot_root = get_depot_root(workspace_dir)
    if not depot_root:
        log.error('No depot root configured. Run "git p4son init" first.')
        return 1
    log.success(depot_root)

    log.heading('Checking git workspace')
    dirty_files = get_dirty_files(workspace_dir)
    if dirty_files:
        for filename, change in dirty_files:
            log.file_change(filename, change)
        log.error('Workspace is not clean')
        return 1
    log.success('clean')

    log.heading('Checking p4 workspace')
    opened_files = p4_get_opened_files(depot_root, workspace_dir)
    if opened_files:
        for filename, change in opened_files:
            log.file_change(filename, change)
        log.error('Workspace is not clean')
        return 1
    log.success('Clean')

    last_changelist_label = 'last synced'
    log.heading(f'Finding {last_changelist_label} changelist')
    last_changelist = git_changelist_of_last_sync(workspace_dir)
    if last_changelist is not None:
        log.success(f'CL {last_changelist}')
    else:
        log.warning('No previous sync found')

    if args.changelist is not None and args.changelist.lower() == 'last-synced':
        if last_changelist is None:
            log.error('No previous sync found, cannot use "last-synced"')
            return 1
        if not p4_sync(last_changelist, last_changelist_label, args.force,
                       depot_root, workspace_dir):
            return 1
        return 0

    # No argument means sync to latest
    if args.changelist is None:
        log.heading('Finding latest changelist')
        changelist = get_latest_changelist(depot_root, workspace_dir)
        log.success(f'CL {changelist}')
        changelist_label = 'latest'
    else:
        changelist_label = 'specified'
        # Convert changelist string to integer
        try:
            changelist = int(args.changelist)
        except ValueError:
            log.error(f'Invalid changelist number: {args.changelist}')
            return 1

    if last_changelist == changelist:
        log.info(f'Already at CL {last_changelist}, nothing to do.')
        return 0

    # Check if trying to sync to an older changelist
    if last_changelist is not None and changelist < last_changelist:
        if not args.force:
            log.error(
                f'Cannot sync to CL {changelist} '
                f'(currently at CL {last_changelist}) without --force.')
            return 1
        else:
            log.warning(
                f'Syncing to older CL {changelist} '
                f'(currently at CL {last_changelist}) with --force')

    if last_changelist is not None:
        if not p4_sync(last_changelist, last_changelist_label, args.force,
                       depot_root, workspace_dir):
            return 1

    if not p4_sync(changelist, changelist_label, args.force,
                   depot_root, workspace_dir):
        return 1

    log.heading('Committing git changes')
    dirty_files = get_dirty_files(workspace_dir)
    if dirty_files:
        add_all_files(workspace_dir)

    commit_msg = f'git-p4son: p4 sync {depot_root}/...@{changelist}'
    commit(commit_msg, workspace_dir, allow_empty=True)
    log.success(f'Committed {len(dirty_files)} files')

    return 0
