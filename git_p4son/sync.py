"""
Sync command implementation for git-p4son.
"""

import argparse
import os
import re
import subprocess
import sys
import time
from timeit import default_timer as timer
from datetime import timedelta
from typing import IO

from .common import ensure_workspace, run, run_with_output
from .changelist_store import resolve_changelist


def echo_output_to_stream(line: str, stream: IO[str]) -> None:
    """Echo a line to a stream."""
    print(line, file=stream)


def get_writable_files(stderr_lines: list[str]) -> list[str]:
    """Extract writable files from p4 sync stderr output."""
    cant_clobber_prefix = "Can't clobber writable file "
    writable_files = []
    for line in stderr_lines:
        if not line.startswith(cant_clobber_prefix):
            continue
        writable_file = line[len(cant_clobber_prefix):]
        writable_files.append(writable_file.rstrip())
    return writable_files


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


def green_text(s: str) -> str:
    """Format text in green color."""
    return f'\033[92m{s}\033[0m'


class SyncStats:
    """Statistics for sync operations."""

    def __init__(self) -> None:
        self.count: int = 0


def readable_file_size(num: float, suffix: str = "B") -> str:
    """Convert bytes to human readable format."""
    for unit in ('', 'Ki', 'Mi', 'Gi', 'Ti', 'Pi', 'Ei', 'Zi'):
        if abs(num) < 1024.0:
            return f'{num:3.1f}{unit}{suffix}'
        num /= 1024.0
    return f'{num:.1f}Yi{suffix}'


class P4SyncOutputProcessor:
    """Process p4 sync output in real-time."""

    def __init__(self, file_count_to_sync: int) -> None:
        self.start_timestamp: float = timer()
        self.synced_file_count: int = 0
        self.file_count_to_sync: int = file_count_to_sync
        self.stats: dict[str, SyncStats] = {}
        for mode in ['add', 'del', 'upd', 'clb']:
            self.stats[mode] = SyncStats()

    def __call__(self, line: str, stream: IO[str]) -> None:
        if re.search(r"//...@\d+ - file\(s\) up-to-date\.", line):
            print('All files are up to date')
            return

        mode, filename = parse_p4_sync_line(line)
        if not mode or not filename:
            print(f'Unparsable line: {line}')
            return

        if mode in self.stats:
            self.stats[mode].count += 1
        self.synced_file_count += 1

        print('{}: {}'.format(green_text(mode), filename))

        indentation = '     '
        if self.file_count_to_sync >= 0:
            print('{}progress: {} / {}'.format(indentation,
                                               self.synced_file_count,
                                               self.file_count_to_sync))

        print('{}sync stats {}'.format(indentation, self.get_sync_stats()))

    def get_sync_stats(self) -> str:
        """Get current sync statistics."""
        duration_sec = timer() - self.start_timestamp
        duration = timedelta(seconds=duration_sec)

        synced_count = self.stats['add'].count + \
            self.stats['upd'].count - self.stats['clb'].count

        return f'file count {synced_count}, time {duration}'

    def print_stats(self) -> None:
        """Print final sync statistics."""
        sync_stats = self.get_sync_stats()
        print(f'Sync stats: {sync_stats}')

        for mode, stat in self.stats.items():
            print(f'{mode}')
            print(f'  count: {stat.count}')


def p4_force_sync_file(changelist: int, filename: str, workspace_dir: str) -> int:
    """Force sync a single file."""
    output_processor = P4SyncOutputProcessor(-1)
    res = run_with_output(['p4', 'sync', '-f', '%s@%s' %
                          (filename, changelist)], cwd=workspace_dir, on_output=output_processor)
    output_processor.print_stats()
    return res.returncode


def get_file_count_to_sync(changelist: int, workspace_dir: str) -> int:
    """Get the number of files that need to be synced."""
    res = run(['p4', 'sync', '-n', '//...@%s' %
              (changelist)], cwd=workspace_dir)

    if res.returncode != 0:
        return -1

    return len(res.stdout)


def p4_sync(changelist: int, force: bool, workspace_dir: str) -> bool:
    """Sync files from Perforce."""
    file_count_to_sync = get_file_count_to_sync(changelist, workspace_dir)
    if file_count_to_sync < 0:
        return False
    if file_count_to_sync == 0:
        print('All files are up to date')
        return True
    print(f'Syncing {file_count_to_sync} files')

    output_processor = P4SyncOutputProcessor(file_count_to_sync)
    res = run_with_output(['p4', 'sync', '//...@%s' %
                          (changelist)], cwd=workspace_dir, on_output=output_processor)
    output_processor.print_stats()
    if res.returncode == 0:
        return True

    writable_files = get_writable_files(res.stderr)
    print('Found %d writable files' % len(writable_files))
    if force:
        for filename in writable_files:
            if p4_force_sync_file(changelist, filename, workspace_dir) != 0:
                return False
    else:
        print('Leaving files as is, use --force to force sync')
        for filename in writable_files:
            print(filename)
        return False

    return True


def p4_is_workspace_clean(workspace_dir: str) -> bool:
    """Check if Perforce workspace is clean."""
    res = run_with_output(['p4', 'opened'], cwd=workspace_dir,
                          on_output=echo_output_to_stream)
    if res.returncode != 0:
        print('Failed to run p4 opened')
        return False

    local_changes = res.stdout
    return len(local_changes) == 0


def git_is_workspace_clean(workspace_dir: str) -> bool:
    """Check if git workspace is clean."""
    res = run_with_output(['git', 'status', '--porcelain'], cwd=workspace_dir,
                          on_output=echo_output_to_stream)
    if res.returncode != 0:
        print('Failed to run git status')
        return False

    local_changes = res.stdout
    return len(local_changes) == 0


def git_add_all_files(workspace_dir: str) -> bool:
    """Add all files to git."""
    res = run_with_output(['git', 'add', '.'], cwd=workspace_dir,
                          on_output=echo_output_to_stream)
    return res.returncode == 0


def git_commit(message: str, workspace_dir: str, allow_empty: bool = False) -> bool:
    """Commit changes to git."""
    args = ['commit', '-m', message]
    if allow_empty:
        args.append('--allow-empty')
    res = run_with_output(['git'] + args,
                          cwd=workspace_dir, on_output=echo_output_to_stream)
    return res.returncode == 0


def git_changelist_of_last_commit(workspace_dir: str) -> int | None:
    """Get the changelist number from the last commit message."""
    res = run_with_output(['git', 'log', '--oneline', '-1', '--pretty="%s"'],
                          cwd=workspace_dir, on_output=echo_output_to_stream)
    if res.returncode != 0 or len(res.stdout) == 0:
        return None

    msg = res.stdout[0]
    pattern = r"^(\d+|pergit|git-p4son): p4 sync //\.\.\.@(\d+)$"
    match = re.search(pattern, msg)
    if match:
        return int(match.group(1))
    else:
        return None


def get_latest_changelist_affecting_workspace(workspace_dir: str) -> tuple[int, int | None]:
    """
    Get the latest changelist that affects files in the client's workspace view.
    This finds the most recent changelist that would be pulled by 'p4 sync'.

    Args:
        workspace_dir: The workspace directory

    Returns:
        Tuple of (returncode, changelist_number or None)
    """
    # First, get the client name
    res = run(['p4', 'info'], cwd=workspace_dir)
    if res.returncode != 0:
        return (res.returncode, None)

    client_name = None
    for line in res.stdout:
        if line.startswith('Client name:'):
            client_name = line.split(':', 1)[1].strip()
            break

    if not client_name:
        return (1, None)

    # Get the latest changelist that affects files in the client's workspace view
    # Using #head to get the latest revisions in the depot that match the client view
    res = run(['p4', 'changes', '-m1', '-s', 'submitted',
              f'//{client_name}/...#head'], cwd=workspace_dir)
    if res.returncode != 0 or len(res.stdout) == 0:
        return (res.returncode, None)

    # Parse the changelist number from the output
    # Format is typically: "Change 12345 on 2023/01/01 by user@workspace 'description'"
    line = res.stdout[0]
    match = re.search(r'Change (\d+)', line)
    if match:
        return (0, int(match.group(1)))
    else:
        return (1, None)


def sync_command(args: argparse.Namespace) -> int:
    """
    Execute the sync command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = ensure_workspace()

    if not git_is_workspace_clean(workspace_dir):
        print('git status shows that workspace is not clean, aborting')
        return 1
    print('')

    if not p4_is_workspace_clean(workspace_dir):
        print('p4 opened shows that workspace is not clean, aborting')
        return 1
    print('')

    # Resolve changelist alias (skip special keywords)
    if args.changelist.lower() not in ('latest', 'last-synced'):
        resolved = resolve_changelist(args.changelist, workspace_dir)
        if resolved is None:
            return 1
        args.changelist = resolved

    last_changelist = git_changelist_of_last_commit(workspace_dir)
    if args.changelist.lower() == 'last-synced':
        if not p4_sync(last_changelist, args.force, workspace_dir):
            print('Failed to sync files from perforce')
            return 1
        return 0

    # Handle "latest" keyword
    if args.changelist.lower() == 'latest':
        returncode, latest_changelist = get_latest_changelist_affecting_workspace(
            workspace_dir)
        if returncode != 0:
            print('Failed to get latest changelist affecting workspace',
                  file=sys.stderr)
            return 1
        if latest_changelist is None:
            print('No changelists found affecting workspace', file=sys.stderr)
            return 1
        print(f'Latest changelist affecting workspace: {latest_changelist}')
        args.changelist = latest_changelist
    else:
        # Convert changelist string to integer for comparison
        try:
            args.changelist = int(args.changelist)
        except ValueError:
            print('Invalid changelist number: %s' %
                  args.changelist, file=sys.stderr)
            return 1

    if last_changelist == args.changelist:
        print('Changelist of last commit is %d, nothing to do, aborting '
              % last_changelist)
        return 0

    # Check if trying to sync to an older changelist
    if last_changelist is not None and args.changelist < last_changelist:
        if not args.force:
            print('Cannot sync to older changelist %d (currently at %d) without --force flag'
                  % (args.changelist, last_changelist))
            print('Use --force to override this safety check')
            return 1
        else:
            print('Warning: Syncing to older changelist %d (currently at %d) with --force flag'
                  % (args.changelist, last_changelist))
    print('')

    if last_changelist != None:
        if not p4_sync(last_changelist, args.force, workspace_dir):
            print('Failed to sync files from perforce')
            return 1
        print('')

    if not p4_sync(args.changelist, args.force, workspace_dir):
        print('Failed to sync files from perforce')
        return 1
    print('')

    if not git_is_workspace_clean(workspace_dir):
        if not git_add_all_files(workspace_dir):
            print('Failed to add all files to git')
            return 1
        print('')

    commit_msg = 'git-p4son: p4 sync //...@%s' % (args.changelist)
    if not git_commit(commit_msg, workspace_dir, allow_empty=True):
        print('Failed to commit files to git')
        return 1
    print('')

    print('Finished with success')
    return 0
