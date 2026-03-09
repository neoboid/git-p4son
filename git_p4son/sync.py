"""
Sync command implementation for git-p4son.
"""

import argparse
import re
from typing import IO

from .common import CommandError, RunError, run, run_with_output
from .config import get_depot_root
from .log import log


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

    def __init__(self, file_count_to_sync: int) -> None:
        self.synced_file_count: int = 0
        self.file_count_to_sync: int = file_count_to_sync
        self.stats: dict[str, int] = {
            mode: 0 for mode in ['add', 'del', 'upd', 'clb']}

    def __call__(self, line: str, stream: IO[str]) -> None:
        if re.search(r"@\d+ - file\(s\) up-to-date\.", line):
            log.info('all files up to date')
            return

        mode, filename = parse_p4_sync_line(line)
        if not mode or not filename:
            log.verbose(f'Unparsable line: {line}')
            return

        self.stats[mode] += 1
        self.synced_file_count += 1

        if self.file_count_to_sync >= 0:
            log.verbose(
                f'{mode}: {filename}  ({self.synced_file_count}/{self.file_count_to_sync})')
        else:
            log.verbose(f'{mode}: {filename}')

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


def p4_force_sync_file(changelist: int, filename: str, workspace_dir: str) -> None:
    """Force sync a single file."""
    output_processor = P4SyncOutputProcessor(-1)
    result = run_with_output(
        ['p4', 'sync', '-f', f'{filename}@{changelist}'],
        cwd=workspace_dir, on_output=output_processor)
    log.info(output_processor.get_summary())
    if result.elapsed:
        log.elapsed(result.elapsed)


def get_file_count_to_sync(changelist: int, depot_root: str,
                           workspace_dir: str) -> int:
    """Get the number of files that need to be synced."""
    res = run(['p4', 'sync', '-n', f'{depot_root}/...@{changelist}'],
              cwd=workspace_dir)
    return len(res.stdout)


def p4_sync(changelist: int, label: str, force: bool, depot_root: str,
            workspace_dir: str) -> bool:
    """Sync files from Perforce.

    Returns True on success, False if writable files were found without --force.
    Raises CommandError on actual command failures.
    """
    log.heading(f'Syncing to {label} CL ({changelist})')
    file_count_to_sync = get_file_count_to_sync(changelist, depot_root,
                                                workspace_dir)
    if file_count_to_sync == 0:
        log.success('All files up to date')
        return True
    log.info(f'{file_count_to_sync} files to sync')

    output_processor = P4SyncOutputProcessor(file_count_to_sync)
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


def p4_get_opened_files(depot_root: str, workspace_dir: str) -> list[tuple[str, str]]:
    """Return list of (filename, change_type) tuples for files opened in Perforce."""
    res = run_with_output(
        ['p4', 'opened', f'{depot_root}/...'], cwd=workspace_dir)
    files = []
    for line in res.stdout:
        # Format: "//depot/path/file#rev - <action> change ..."
        parts = line.split(' - ', 1)
        if len(parts) < 2:
            continue
        depot_path = parts[0].split('#')[0]
        action = parts[1].split()[0]
        if action in ('add', 'move/add'):
            change = 'add'
        elif action in ('delete', 'move/delete'):
            change = 'delete'
        else:
            change = 'modify'
        files.append((depot_path, change))
    return files


def git_get_dirty_files(workspace_dir: str) -> list[tuple[str, str]]:
    """Return list of (filename, change_type) tuples for dirty files in git."""
    res = run_with_output(['git', 'status', '--porcelain'], cwd=workspace_dir)
    files = []
    for line in res.stdout:
        status = line[:2].strip()
        filename = line[3:]
        if status == 'A':
            files.append((filename, 'add'))
        elif status == 'D':
            files.append((filename, 'delete'))
        elif status == '??':
            files.append((filename, 'untracked'))
        else:
            files.append((filename, 'modify'))
    return files


def git_add_all_files(workspace_dir: str) -> None:
    """Add all files to git."""
    run_with_output(['git', 'add', '.'], cwd=workspace_dir)


def git_commit(message: str, workspace_dir: str, allow_empty: bool = False) -> None:
    """Commit changes to git."""
    args = ['commit', '-m', message]
    if allow_empty:
        args.append('--allow-empty')
    run_with_output(['git'] + args, cwd=workspace_dir)


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


def get_latest_changelist(depot_root: str, workspace_dir: str) -> int:
    """Get the latest submitted changelist affecting the depot root."""
    res = run(['p4', 'changes', '-m1', '-s', 'submitted',
              f'{depot_root}/...#head'], cwd=workspace_dir)
    if not res.stdout:
        raise CommandError('No changelists found affecting workspace')

    # Parse the changelist number from the output
    line = res.stdout[0]
    match = re.search(r'Change (\d+)', line)
    if not match:
        raise CommandError(f'Failed to parse changelist from: {line}')
    return int(match.group(1))


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
    dirty_files = git_get_dirty_files(workspace_dir)
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
    dirty_files = git_get_dirty_files(workspace_dir)
    if dirty_files:
        git_add_all_files(workspace_dir)

    commit_msg = f'git-p4son: p4 sync {depot_root}/...@{changelist}'
    git_commit(commit_msg, workspace_dir, allow_empty=True)
    log.success(f'Committed {len(dirty_files)} files')

    return 0
