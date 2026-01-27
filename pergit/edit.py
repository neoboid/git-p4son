"""
Edit command implementation for pergit.
"""

import argparse
import re
import sys
from .common import ensure_workspace, run


class LocalChanges:
    """Container for local git changes."""

    def __init__(self) -> None:
        self.adds: list[str] = []
        self.mods: list[str] = []
        self.dels: list[str] = []
        self.moves: list[tuple[str, str]] = []


def check_file_status(filename: str, workspace_dir: str) -> str | None:
    """
    Check if a file is already checked out in Perforce and return its changelist.

    Args:
        filename: The file to check
        workspace_dir: The workspace directory

    Returns:
        changelist_number or None
        changelist_number is None if file is not checked out
    """
    res = run(['p4', 'opened', filename], cwd=workspace_dir)

    # Check if file is not opened (p4 opened always returns 0, so check output)
    if not res.stdout or any('file(s) not opened on this client' in line for line in res.stdout):
        return None

    # Parse the output to extract changelist number
    # Format: "//depot/path/file#1 - edit change 12345 (text) by user@workspace"
    for line in res.stdout:
        if '- edit default change ' in line:
            return 'default'
        if '- edit change ' in line:
            # Extract changelist number using regex
            match = re.search(r'change (\d+)', line)
            if match:
                return match.group(1)

    # If we get here, file is checked out but we couldn't parse the changelist
    return None


def find_common_ancestor(branch1: str, branch2: str, workspace_dir: str) -> tuple[int, str | None]:
    """
    Find the common ancestor commit between two branches.

    Args:
        branch1: First branch name
        branch2: Second branch name
        workspace_dir: The git workspace directory

    Returns:
        Tuple of (returncode, common_ancestor_commit_hash or None)
    """
    res = run(['git', 'merge-base', branch1, branch2], cwd=workspace_dir)
    if res.returncode != 0:
        return (res.returncode, None)

    # The output should be a single commit hash
    if not res.stdout or len(res.stdout) != 1:
        return (1, None)

    return (0, res.stdout[0].strip())


def get_local_git_changes(base_branch: str, workspace_dir: str) -> tuple[int, LocalChanges | None]:
    """
    Get local git changes between base_branch and HEAD using common ancestor logic.

    Args:
        base_branch: The base branch to compare against
        workspace_dir: The git workspace directory

    Returns:
        Tuple of (returncode, LocalChanges object or None)
    """
    # Always find common ancestor between base_branch and current HEAD
    returncode, ancestor = find_common_ancestor(
        base_branch, 'HEAD', workspace_dir)
    if returncode != 0:
        print(
            f'Failed to find common ancestor between {base_branch} and HEAD', file=sys.stderr)
        return (returncode, None)

    if not ancestor:
        print(f'No common ancestor found between {base_branch} and HEAD. '
              f'This usually means the branches have completely different histories.', file=sys.stderr)
        return (1, None)

    # Diff base_branch against the common ancestor to find files that changed on base_branch
    # but not on the current branch
    res = run(['git', 'diff', '--name-status', '{}..{}'.format(ancestor, 'HEAD')],
              cwd=workspace_dir)

    if res.returncode != 0:
        return (res.returncode, None)

    changes = LocalChanges()
    renamepattern = r"^r(\d+)$"
    for line in res.stdout:
        tokens = line.split('\t')
        status = tokens[0].lower()
        filename = tokens[1]
        if status == 'm':
            changes.mods.append(filename)
        elif status == 'd':
            changes.dels.append(filename)
        elif status == 'a':
            changes.adds.append(filename)
        elif re.search(renamepattern, status):
            from_filename = filename
            to_filename = tokens[2]
            changes.moves.append((from_filename, to_filename))
        else:
            print('Unknown git status in "{}"'.format(line), file=sys.stderr)
            return (1, None)

    return (0, changes)


def edit_command(args: argparse.Namespace) -> int:
    """
    Execute the edit command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = ensure_workspace()

    changelist = args.changelist

    returncode, changes = get_local_git_changes(
        args.base_branch, workspace_dir)
    if returncode != 0:
        print('Failed to get a list of changed files', file=sys.stderr)
        return returncode

    # Process added files
    for filename in changes.adds:
        res = run(['p4', 'add', '-c', changelist, filename],
                  cwd=workspace_dir, dry_run=args.dry_run)
        if res.returncode != 0:
            print('Failed to add file to perforce', file=sys.stderr)
            return res.returncode

    # Process modified files
    for filename in changes.mods:
        # Check if file is already checked out
        current_changelist = check_file_status(filename, workspace_dir)

        if current_changelist is None:
            # File is not checked out, use p4 edit
            res = run(['p4', 'edit', '-c', changelist, filename],
                      cwd=workspace_dir, dry_run=args.dry_run)
            if res.returncode != 0:
                print('Failed to open file for edit in perforce', file=sys.stderr)
                return res.returncode
        elif current_changelist != changelist:
            # File is checked out in different changelist, use p4 reopen
            res = run(['p4', 'reopen', '-c', changelist, filename],
                      cwd=workspace_dir, dry_run=args.dry_run)
            if res.returncode != 0:
                print('Failed to reopen file in perforce', file=sys.stderr)
                return res.returncode
        # If current_changelist == changelist, file is already in correct changelist, do nothing

    # Process deleted files
    for filename in changes.dels:
        res = run(['p4', 'delete', '-c', changelist, filename],
                  cwd=workspace_dir, dry_run=args.dry_run)
        if res.returncode != 0:
            print('Failed to delete file from perforce', file=sys.stderr)
            return res.returncode

    # Process moved/renamed files
    for from_filename, to_filename in changes.moves:
        res = run(['p4', 'delete', '-c', changelist, from_filename],
                  cwd=workspace_dir, dry_run=args.dry_run)
        if res.returncode != 0:
            print('Failed to delete from-file in perforce', file=sys.stderr)
            return res.returncode
        res = run(['p4', 'add', '-c', changelist, to_filename],
                  cwd=workspace_dir, dry_run=args.dry_run)
        if res.returncode != 0:
            print('Failed to add file to-file to perforce', file=sys.stderr)
            return res.returncode

    return 0
