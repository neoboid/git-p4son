"""
Reusable library functions for git-p4son.

Functions for managing Perforce changelists, opening files for edit,
shelving, and Swarm review integration.
"""

import re
from .common import CommandError, run
from .list_changes import get_enumerated_commit_lines_since


# ---------------------------------------------------------------------------
# Changelist functions (from changelist.py)
# ---------------------------------------------------------------------------

def create_changelist(message: str, base_branch: str, workspace_dir: str, dry_run: bool = False) -> str | None:
    """
    Create a new Perforce changelist with the given message and
    enumerated git commits as description.

    Args:
        message: User-provided changelist description
        base_branch: The base branch to compare against for commit list
        workspace_dir: The workspace directory
        dry_run: If True, don't actually create the changelist

    Returns:
        The changelist number as a string, or None for dry run.
    """
    # Build description: user message + enumerated commits
    commit_lines = get_enumerated_commit_lines_since(base_branch, workspace_dir)

    description_lines = message.splitlines()
    if commit_lines:
        description_lines += ['', 'Changes included:'] + commit_lines

    if dry_run:
        print(f"Would create new changelist with description:")
        print('\n'.join(description_lines))
        return None

    # Prepare the changelist spec content
    tabbed_description = "\n\t".join(description_lines)
    spec_content = f"Change: new\n\nDescription:\n\t{tabbed_description}\n"

    # Create the changelist using p4 change -i
    result = run(['p4', 'change', '-i'], cwd=workspace_dir, input=spec_content)

    # Extract changelist number from output
    # Format: "Change 12345 created."
    for line in result.stdout:
        if 'Change' in line and 'created' in line:
            match = re.search(r'Change (\d+) created', line)
            if match:
                return match.group(1)

    raise CommandError(
        'Failed to extract changelist number from p4 change output',
        stderr=result.stdout,
    )


def get_changelist_spec(changelist_nr: str, workspace_dir: str) -> str:
    """
    Fetch the changelist spec from Perforce.

    Args:
        changelist_nr: The changelist number
        workspace_dir: The workspace directory

    Returns:
        The spec text as a string.
    """
    result = run(['p4', 'change', '-o', changelist_nr], cwd=workspace_dir)
    return '\n'.join(result.stdout) + '\n'


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


def find_end_of_indented_section(lines: list[str], start: int) -> int:
    """
    Find the end of a tab-indented section.

    Args:
        lines: List of lines to search
        start: Index to start searching from

    Returns:
        Index of the first non-tab-indented line, or len(lines) if all remaining lines are indented.
    """
    i = start
    while i < len(lines) and lines[i].startswith('\t'):
        i += 1
    return i


def extract_description_lines(spec_text: str) -> list[str]:
    """
    Extract the Description field from a p4 changelist spec.

    The spec has tab-indented continuation lines under the Description: header.

    Returns:
        List of description lines with tabs stripped.
    """
    lines = spec_text.splitlines()
    start = find_line_starting_with(lines, 'Description:') + 1
    end = find_end_of_indented_section(lines, start)
    return [line[1:] for line in lines[start:end]]


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
    desc_line = find_line_starting_with(lines, 'Description:')

    if desc_line >= len(lines):
        return spec_text

    desc_end = find_end_of_indented_section(lines, desc_line + 1)

    result_lines = lines[:desc_line + 1]
    result_lines.extend('\t' + line for line in new_description_lines)
    result_lines.extend(lines[desc_end:])

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


def update_changelist(changelist_nr: str, base_branch: str, workspace_dir: str, dry_run: bool = False) -> None:
    """
    Update an existing Perforce changelist by appending new commits
    to the enumerated commit list in the description.

    Args:
        changelist_nr: The changelist number to update
        base_branch: The base branch to compare against for commit list
        workspace_dir: The workspace directory
        dry_run: If True, don't actually update the changelist
    """
    # Fetch existing spec
    spec_text = get_changelist_spec(changelist_nr, workspace_dir)

    # Extract and split description into lines
    description_lines = extract_description_lines(spec_text)
    message_lines, old_commit_lines, trailing_lines = split_description_lines(
        description_lines)

    # Generate new commit list, continuing from existing count
    start_number = len(old_commit_lines) + 1
    new_commit_lines = get_enumerated_commit_lines_since(
        base_branch, workspace_dir, start_number=start_number)

    # Rebuild description: message + old commits + new commits + trailing
    new_description_lines = message_lines + \
        old_commit_lines + new_commit_lines + trailing_lines

    if dry_run:
        print(f"Would update changelist {changelist_nr} with description:")
        print('\n'.join(new_description_lines))
        return

    # Replace description in spec and submit
    new_spec = replace_description_in_spec(spec_text, new_description_lines)
    run(['p4', 'change', '-i'], cwd=workspace_dir, input=new_spec)


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


def find_common_ancestor(branch1: str, branch2: str, workspace_dir: str) -> str:
    """
    Find the common ancestor commit between two branches.

    Args:
        branch1: First branch name
        branch2: Second branch name
        workspace_dir: The git workspace directory

    Returns:
        The common ancestor commit hash.
    """
    res = run(['git', 'merge-base', branch1, branch2], cwd=workspace_dir)
    if not res.stdout or len(res.stdout) != 1:
        raise CommandError('git merge-base returned unexpected output')
    return res.stdout[0].strip()


def get_local_git_changes(base_branch: str, workspace_dir: str) -> LocalChanges:
    """
    Get local git changes between base_branch and HEAD using common ancestor logic.

    Args:
        base_branch: The base branch to compare against
        workspace_dir: The git workspace directory

    Returns:
        LocalChanges object with adds, mods, dels, and moves.
    """
    ancestor = find_common_ancestor(base_branch, 'HEAD', workspace_dir)

    res = run(['git', 'diff', '--name-status', '{}..{}'.format(ancestor, 'HEAD')],
              cwd=workspace_dir)

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
            raise CommandError('Unknown git status in "{}"'.format(line))

    return changes


def open_changes_for_edit(changelist: str, base_branch: str, workspace_dir: str, dry_run: bool = False) -> None:
    """
    Get local git changes and open them for edit in a Perforce changelist.

    Args:
        changelist: The changelist number to add files to
        base_branch: The base branch to compare against
        workspace_dir: The workspace directory
        dry_run: If True, don't actually execute commands
    """
    changes = get_local_git_changes(base_branch, workspace_dir)
    include_changes_in_changelist(changes, changelist, workspace_dir, dry_run)


def include_changes_in_changelist(changes: LocalChanges, changelist: str, workspace_dir: str, dry_run: bool = False) -> None:
    """
    Process local git changes by adding them to a Perforce changelist.

    Args:
        changes: LocalChanges object containing adds, mods, dels, moves
        changelist: The changelist number to update
        workspace_dir: The workspace directory
        dry_run: If True, don't actually execute commands
    """
    # Process added files
    for filename in changes.adds:
        run(['p4', 'add', '-c', changelist, filename],
            cwd=workspace_dir, dry_run=dry_run)

    # Process modified files
    for filename in changes.mods:
        current_changelist = check_file_status(filename, workspace_dir)

        if current_changelist is None:
            run(['p4', 'edit', '-c', changelist, filename],
                cwd=workspace_dir, dry_run=dry_run)
        elif current_changelist != changelist:
            run(['p4', 'reopen', '-c', changelist, filename],
                cwd=workspace_dir, dry_run=dry_run)
        # If current_changelist == changelist, file is already in correct changelist, do nothing

    # Process deleted files
    for filename in changes.dels:
        run(['p4', 'delete', '-c', changelist, filename],
            cwd=workspace_dir, dry_run=dry_run)

    # Process moved/renamed files
    for from_filename, to_filename in changes.moves:
        run(['p4', 'delete', '-c', changelist, from_filename],
            cwd=workspace_dir, dry_run=dry_run)
        run(['p4', 'add', '-c', changelist, to_filename],
            cwd=workspace_dir, dry_run=dry_run)


# ---------------------------------------------------------------------------
# Review / shelve functions (from review.py)
# ---------------------------------------------------------------------------

def p4_shelve_changelist(changelist: str, workspace_dir: str, dry_run: bool = False) -> None:
    """
    Shelve a changelist to make it available for review.

    Args:
        changelist: The changelist number to shelve
        workspace_dir: The workspace directory
        dry_run: If True, don't actually shelve
    """
    run(['p4', 'shelve', '-f', '-Af', '-c', changelist],
        cwd=workspace_dir, dry_run=dry_run)


def add_review_keyword_to_changelist(changelist: str, workspace_dir: str, dry_run: bool = False) -> None:
    """
    Add the #review keyword to a changelist description.

    Args:
        changelist: The changelist number to update
        workspace_dir: The workspace directory
        dry_run: If True, don't actually update
    """
    # Get current changelist description
    res = run(['p4', 'change', '-o', changelist], cwd=workspace_dir)

    lines = res.stdout
    desc_start = find_line_starting_with(lines, 'Description:')
    if desc_start >= len(lines):
        raise CommandError('No Description: field found in changelist spec')

    desc_end = find_end_of_indented_section(lines, desc_start + 1)

    # Check if #review is already in the description
    if any('#review' in line for line in lines[desc_start:desc_end]):
        print(f'Changelist {changelist} already has #review keyword')
        return

    if dry_run:
        print(f"Would add #review keyword to changelist {changelist}")
        return

    # Insert #review at the end of the description, preceded by a blank line
    lines[desc_end:desc_end] = ['\t', '\t#review']

    run(['p4', 'change', '-i'], cwd=workspace_dir,
        input='\n'.join(lines))
