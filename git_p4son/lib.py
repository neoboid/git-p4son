"""
Reusable library functions for git-p4son.

Functions for managing Perforce changelists, opening files for edit,
shelving, and Swarm review integration.
"""

import re
from .common import CommandError, run
from .list_changes import get_enumerated_commit_lines_since
from .log import log


# ---------------------------------------------------------------------------
# Changelist functions (from changelist.py)
# ---------------------------------------------------------------------------

def create_changelist(message: str, base_branch: str, workspace_dir: str, dry_run: bool = False) -> str | None:
    """Create a new Perforce changelist with the given message and enumerated git commits."""
    # Build description: user message + enumerated commits
    commit_lines = get_enumerated_commit_lines_since(
        base_branch, workspace_dir)

    description_lines = message.splitlines()
    if commit_lines:
        description_lines += ['', 'Changes included:'] + commit_lines

    if dry_run:
        log.info("Would create new changelist with description:")
        log.info('\n'.join(description_lines))
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
    )


def get_changelist_spec(changelist_nr: str, workspace_dir: str) -> str:
    """Fetch the changelist spec from Perforce."""
    result = run(['p4', 'change', '-o', changelist_nr], cwd=workspace_dir)
    return '\n'.join(result.stdout) + '\n'


def find_line_starting_with(lines: list[str], prefix: str) -> int:
    """Find the index of the first line starting with prefix, or len(lines) if not found."""
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            return i
    return len(lines)


def find_end_of_indented_section(lines: list[str], start: int) -> int:
    """Find the end of a tab-indented section starting at the given index."""
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
    """Replace the Description field in a p4 changelist spec."""
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
    """Split description into (message_lines, commit_lines, trailing_lines)."""
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
    """Update an existing changelist by appending new commits to the description."""
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
        log.info(f"Would update changelist {changelist_nr} with description:")
        log.info('\n'.join(new_description_lines))
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


def get_changelist_for_file(filename: str, workspace_dir: str) -> str | None:
    """Return the changelist a file is opened in, or None if not opened."""
    res = run(['p4', 'opened', filename], cwd=workspace_dir)

    # Check if file is not opened (p4 opened always returns 0, so check output)
    if not res.stdout or any('file(s) not opened on this client' in line for line in res.stdout):
        return None

    # Parse the output to extract changelist number
    # Format: "//depot/path/file#1 - <action> change 12345 (type) by user@workspace"
    # where <action> is edit, add, delete, move/add, move/delete, etc.
    for line in res.stdout:
        if ' default change ' in line:
            return 'default'
        match = re.search(r' change (\d+) ', line)
        if match:
            return match.group(1)

    # If we get here, file is opened but we couldn't parse the changelist
    return None


def find_common_ancestor(branch1: str, branch2: str, workspace_dir: str) -> str:
    """Find the common ancestor commit between two branches."""
    res = run(['git', 'merge-base', branch1, branch2], cwd=workspace_dir)
    if not res.stdout or len(res.stdout) != 1:
        raise CommandError('git merge-base returned unexpected output')
    return res.stdout[0].strip()


def get_local_git_changes(base_branch: str, workspace_dir: str) -> LocalChanges:
    """Get local git changes between base_branch and HEAD."""
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
    """Get local git changes and open them for edit in a Perforce changelist."""
    changes = get_local_git_changes(base_branch, workspace_dir)
    include_changes_in_changelist(changes, changelist, workspace_dir, dry_run)


def _ensure_in_changelist(filename: str, p4_action: str, changelist: str,
                          workspace_dir: str, dry_run: bool) -> None:
    """Ensure a file is opened in the given changelist.

    If the file is not yet opened, run the specified p4 action (add, edit, delete).
    If it's already opened in a different changelist, reopen it.
    If it's already in the correct changelist, do nothing.
    """
    current = get_changelist_for_file(filename, workspace_dir)
    if current is None:
        run(['p4', p4_action, '-c', changelist, filename],
            cwd=workspace_dir, dry_run=dry_run)
    elif current != changelist:
        run(['p4', 'reopen', '-c', changelist, filename],
            cwd=workspace_dir, dry_run=dry_run)


def include_changes_in_changelist(changes: LocalChanges, changelist: str, workspace_dir: str, dry_run: bool = False) -> None:
    """Open local git changes for add/edit/delete in a Perforce changelist."""
    for filename in changes.adds:
        _ensure_in_changelist(filename, 'add', changelist,
                              workspace_dir, dry_run)

    for filename in changes.mods:
        _ensure_in_changelist(filename, 'edit', changelist,
                              workspace_dir, dry_run)

    for filename in changes.dels:
        _ensure_in_changelist(filename, 'delete',
                              changelist, workspace_dir, dry_run)

    for from_filename, to_filename in changes.moves:
        _ensure_in_changelist(from_filename, 'delete',
                              changelist, workspace_dir, dry_run)
        _ensure_in_changelist(to_filename, 'add',
                              changelist, workspace_dir, dry_run)


# ---------------------------------------------------------------------------
# Review / shelve functions (from review.py)
# ---------------------------------------------------------------------------

def p4_shelve_changelist(changelist: str, workspace_dir: str, dry_run: bool = False) -> None:
    """Shelve a changelist to make it available for review."""
    run(['p4', 'shelve', '-f', '-Af', '-c', changelist],
        cwd=workspace_dir, dry_run=dry_run)


def add_review_keyword_to_changelist(changelist: str, workspace_dir: str, dry_run: bool = False) -> None:
    """Add the #review keyword to a changelist description."""
    # Get current changelist description
    res = run(['p4', 'change', '-o', changelist], cwd=workspace_dir)

    lines = res.stdout
    desc_start = find_line_starting_with(lines, 'Description:')
    if desc_start >= len(lines):
        raise CommandError('No Description: field found in changelist spec')

    desc_end = find_end_of_indented_section(lines, desc_start + 1)

    # Check if #review is already in the description
    if any('#review' in line for line in lines[desc_start:desc_end]):
        log.info(f'Changelist {changelist} already has #review keyword')
        return

    if dry_run:
        log.info(f"Would add #review keyword to changelist {changelist}")
        return

    # Insert #review at the end of the description, preceded by a blank line
    lines[desc_end:desc_end] = ['\t', '\t#review']

    run(['p4', 'change', '-i'], cwd=workspace_dir,
        input='\n'.join(lines))
