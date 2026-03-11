"""
Bridge functions that combine git and Perforce operations.
"""

import re
from .common import CommandError, run
from .git import LocalChanges, get_local_changes
from .list_changes import get_enumerated_commit_lines_since
from .log import log
from .perforce import (
    extract_description_lines,
    get_changelist_spec,
    include_changes_in_changelist,
    replace_description_in_spec,
)


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


def open_changes_for_edit(changelist: str, base_branch: str, workspace_dir: str, dry_run: bool = False) -> None:
    """Get local git changes and open them for edit in a Perforce changelist."""
    changes = get_local_changes(base_branch, workspace_dir)
    include_changes_in_changelist(changes, changelist, workspace_dir, dry_run)
