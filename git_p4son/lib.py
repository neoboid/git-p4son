"""
Reusable library functions for git-p4son.

Functions for managing Perforce changelists, opening files for edit,
shelving, and Swarm review integration.
"""

import os
import re
import sys
import subprocess
from .common import run
from .list_changes import get_enumerated_commit_lines_since


# ---------------------------------------------------------------------------
# Changelist functions (from changelist.py)
# ---------------------------------------------------------------------------

def create_changelist(message: str, base_branch: str, workspace_dir: str, dry_run: bool = False) -> tuple[int, str | None]:
    """
    Create a new Perforce changelist with the given message and
    enumerated git commits as description.

    Args:
        message: User-provided changelist description
        base_branch: The base branch to compare against for commit list
        workspace_dir: The workspace directory
        dry_run: If True, don't actually create the changelist

    Returns:
        Tuple of (returncode, changelist_number or None)
    """
    # Build description: user message + enumerated commits
    returncode, commit_lines = get_enumerated_commit_lines_since(
        base_branch, workspace_dir)
    if returncode != 0:
        return (returncode, None)

    description_lines = message.splitlines() + commit_lines

    if dry_run:
        print(f"Would create new changelist with description:")
        print('\n'.join(description_lines))
        return (0, None)

    # Prepare the changelist spec content
    tabbed_description = "\n\t".join(description_lines)
    spec_content = f"Change: new\n\nDescription:\n\t{tabbed_description}\n"

    # Create the changelist using p4 change -i
    try:
        result = subprocess.run(
            ['p4', 'change', '-i'],
            cwd=workspace_dir,
            input=spec_content,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print('Failed to create new changelist', file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return (result.returncode, None)

        # Extract changelist number from output
        # Format: "Change 12345 created."
        changelist_number = None
        for line in result.stdout.splitlines():
            if 'Change' in line and 'created' in line:
                match = re.search(r'Change (\d+) created', line)
                if match:
                    changelist_number = match.group(1)
                    break

        if changelist_number is None:
            print(
                'Failed to extract changelist number from p4 change output',
                file=sys.stderr)
            print('Output:', result.stdout, file=sys.stderr)
            return (1, None)

        return (0, changelist_number)

    except Exception as e:
        print(f'Failed to create new changelist: {e}', file=sys.stderr)
        return (1, None)


def get_changelist_spec(changelist_nr: str, workspace_dir: str) -> tuple[int, str | None]:
    """
    Fetch the changelist spec from Perforce.

    Args:
        changelist_nr: The changelist number
        workspace_dir: The workspace directory

    Returns:
        Tuple of (returncode, spec_text or None)
    """
    try:
        result = subprocess.run(
            ['p4', 'change', '-o', changelist_nr],
            cwd=workspace_dir,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            print(f'Failed to get changelist {changelist_nr}', file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return (result.returncode, None)
        return (0, result.stdout)
    except Exception as e:
        print(
            f'Failed to get changelist {changelist_nr}: {e}', file=sys.stderr)
        return (1, None)


def find_line_starting_with(lines: list[str], prefix: str) -> int:
    """
    Find the index of the first line starting with prefix.

    Args:
        lines: List of lines to search
        prefix: The prefix to match

    Returns:
        Index of the first matching line, or len(lines) if not found.
    """
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            return i
    return len(lines)


def extract_description_lines(spec_text: str) -> list[str]:
    """
    Extract the Description field from a p4 changelist spec.

    The spec has tab-indented continuation lines under the Description: header.

    Returns:
        List of description lines with tabs stripped.
    """
    lines = spec_text.splitlines()
    i = find_line_starting_with(lines, 'Description:') + 1

    # Collect tab-indented description lines
    description_lines = []
    while i < len(lines) and lines[i].startswith('\t'):
        description_lines.append(lines[i][1:])  # strip leading tab
        i += 1

    return description_lines


def replace_description_in_spec(spec_text: str, new_description_lines: list[str]) -> str:
    """
    Replace the Description field in a p4 changelist spec.

    Args:
        spec_text: The full spec text from p4 change -o
        new_description_lines: The new description as a list of lines

    Returns:
        The spec text with the description replaced.
    """
    lines = spec_text.splitlines()
    i = find_line_starting_with(lines, 'Description:')

    if i >= len(lines):
        return spec_text

    # Lines before Description: and the header itself
    result_lines = lines[:i + 1]

    # Add new description lines
    for desc_line in new_description_lines:
        result_lines.append('\t' + desc_line)
    i += 1

    # Skip old description lines (tab-indented)
    while i < len(lines) and lines[i].startswith('\t'):
        i += 1

    # Append remaining lines
    result_lines.extend(lines[i:])

    return '\n'.join(result_lines) + '\n'


def split_description_lines(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """
    Split changelist description lines into the user message, the
    enumerated commit list, and any trailing text.

    The commit list starts at the first line matching "1. " and
    continues as long as lines match "<number>. ". Any text after
    the numbered list is returned as trailing lines.

    Args:
        lines: The description as a list of lines

    Returns:
        Tuple of (message_lines, commit_lines, trailing_lines).
        Each may be an empty list.
    """
    # Find start of numbered list
    start = None
    for i, line in enumerate(lines):
        if line.startswith('1. '):
            start = i
            break
    if start is None:
        return (lines, [], [])

    # Find end of numbered list (consecutive "<number>. " lines)
    end = start + 1
    expected_nr = 2
    for j in range(end, len(lines)):
        if lines[j].startswith(f'{expected_nr}. '):
            expected_nr += 1
            end = j + 1
        else:
            break

    return (lines[:start], lines[start:end], lines[end:])


def update_changelist(changelist_nr: str, base_branch: str, workspace_dir: str, dry_run: bool = False) -> int:
    """
    Update an existing Perforce changelist by appending new commits
    to the enumerated commit list in the description.

    Args:
        changelist_nr: The changelist number to update
        base_branch: The base branch to compare against for commit list
        workspace_dir: The workspace directory
        dry_run: If True, don't actually update the changelist

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Fetch existing spec
    returncode, spec_text = get_changelist_spec(changelist_nr, workspace_dir)
    if returncode != 0:
        return returncode

    # Extract and split description into lines
    description_lines = extract_description_lines(spec_text)
    message_lines, old_commit_lines, trailing_lines = split_description_lines(
        description_lines)

    # Generate new commit list, continuing from existing count
    start_number = len(old_commit_lines) + 1
    returncode, new_commit_lines = get_enumerated_commit_lines_since(
        base_branch, workspace_dir, start_number=start_number)
    if returncode != 0:
        return returncode

    # Rebuild description: message + old commits + new commits + trailing
    new_description_lines = message_lines + \
        old_commit_lines + new_commit_lines + trailing_lines

    if dry_run:
        print(f"Would update changelist {changelist_nr} with description:")
        print('\n'.join(new_description_lines))
        return 0

    # Replace description in spec and submit
    new_spec = replace_description_in_spec(spec_text, new_description_lines)

    try:
        result = subprocess.run(
            ['p4', 'change', '-i'],
            cwd=workspace_dir,
            input=new_spec,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            print(
                f'Failed to update changelist {changelist_nr}', file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return result.returncode
        return 0
    except Exception as e:
        print(
            f'Failed to update changelist {changelist_nr}: {e}', file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Edit functions (from edit.py)
# ---------------------------------------------------------------------------

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


def open_changes_for_edit(changelist: str, base_branch: str, workspace_dir: str, dry_run: bool = False) -> int:
    """
    Get local git changes and open them for edit in a Perforce changelist.

    Args:
        changelist: The changelist number to add files to
        base_branch: The base branch to compare against
        workspace_dir: The workspace directory
        dry_run: If True, don't actually execute commands

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    returncode, changes = get_local_git_changes(base_branch, workspace_dir)
    if returncode != 0:
        print('Failed to get a list of changed files', file=sys.stderr)
        return returncode

    return include_changes_in_changelist(changes, changelist, workspace_dir, dry_run)


def include_changes_in_changelist(changes: LocalChanges, changelist: str, workspace_dir: str, dry_run: bool = False) -> int:
    """
    Process local git changes by adding them to a Perforce changelist.

    Args:
        changes: LocalChanges object containing adds, mods, dels, moves
        changelist: The changelist number to update
        workspace_dir: The workspace directory
        dry_run: If True, don't actually execute commands

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Process added files
    for filename in changes.adds:
        res = run(['p4', 'add', '-c', changelist, filename],
                  cwd=workspace_dir, dry_run=dry_run)
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
                      cwd=workspace_dir, dry_run=dry_run)
            if res.returncode != 0:
                print('Failed to open file for edit in perforce', file=sys.stderr)
                return res.returncode
        elif current_changelist != changelist:
            # File is checked out in different changelist, use p4 reopen
            res = run(['p4', 'reopen', '-c', changelist, filename],
                      cwd=workspace_dir, dry_run=dry_run)
            if res.returncode != 0:
                print('Failed to reopen file in perforce', file=sys.stderr)
                return res.returncode
        # If current_changelist == changelist, file is already in correct changelist, do nothing

    # Process deleted files
    for filename in changes.dels:
        res = run(['p4', 'delete', '-c', changelist, filename],
                  cwd=workspace_dir, dry_run=dry_run)
        if res.returncode != 0:
            print('Failed to delete file from perforce', file=sys.stderr)
            return res.returncode

    # Process moved/renamed files
    for from_filename, to_filename in changes.moves:
        res = run(['p4', 'delete', '-c', changelist, from_filename],
                  cwd=workspace_dir, dry_run=dry_run)
        if res.returncode != 0:
            print('Failed to delete from-file in perforce', file=sys.stderr)
            return res.returncode
        res = run(['p4', 'add', '-c', changelist, to_filename],
                  cwd=workspace_dir, dry_run=dry_run)
        if res.returncode != 0:
            print('Failed to add file to-file to perforce', file=sys.stderr)
            return res.returncode

    return 0


# ---------------------------------------------------------------------------
# Review / shelve functions (from review.py)
# ---------------------------------------------------------------------------

def p4_shelve_changelist(changelist: str, workspace_dir: str, dry_run: bool = False) -> int:
    """
    Shelve a changelist to make it available for review.

    Args:
        changelist: The changelist number to shelve
        workspace_dir: The workspace directory
        dry_run: If True, don't actually shelve

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    res = run(['p4', 'shelve', '-f', '-Af', '-c', changelist],
              cwd=workspace_dir, dry_run=dry_run)

    if res.returncode != 0:
        print('Failed to shelve changelist', file=sys.stderr)

    return res.returncode


def add_review_keyword_to_changelist(changelist: str, workspace_dir: str, dry_run: bool = False) -> tuple[int, bool]:
    """
    Add the #review keyword to a changelist description.

    Args:
        changelist: The changelist number to update
        workspace_dir: The workspace directory
        dry_run: If True, don't actually update

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Get current changelist description
    res = run(['p4', 'change', '-o', changelist], cwd=workspace_dir)
    if res.returncode != 0:
        print('Failed to get changelist description', file=sys.stderr)
        return res.returncode

    # Parse the changelist spec to find description and track its end
    lines = res.stdout
    description_start_idx = None
    description_end_idx = None

    for i, line in enumerate(lines):
        if description_start_idx is None and line.strip() == 'Description:':
            description_start_idx = i
        elif re.match(r'^[A-Za-z].*:$', line.strip()):
            # Description ends when we hit the next field header (non-indented line with a colon)
            description_end_idx = i
            break

    # Check if #review is already in the description
    if description_start_idx is not None:
        # If we didn't find another field header, description goes to end
        if description_end_idx is None:
            description_end_idx = len(lines)

        for desc_line in lines[description_start_idx:description_end_idx]:
            if '#review' in desc_line:
                print(f'Changelist {changelist} already has #review keyword')
                return 0

    # Update the changelist
    if dry_run:
        print(f"Would add #review keyword to changelist {changelist}")
        return 0

    # Add #review as the last line of description
    if description_start_idx is not None:
        # Insert #review before the empty line that ends the description
        lines.insert(description_end_idx, '\t#review')

    try:
        result = subprocess.run(
            ['p4', 'change', '-i'],
            cwd=workspace_dir,
            input='\n'.join(lines),
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print('Failed to update changelist description', file=sys.stderr)
            print(result.stderr, file=sys.stderr)

        return result.returncode

    except Exception as e:
        print(f'Failed to update changelist description: {e}', file=sys.stderr)
        return 1
