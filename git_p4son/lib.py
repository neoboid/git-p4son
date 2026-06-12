"""
Bridge functions that combine git and Perforce operations.
"""

import re
from collections import Counter

from .common import CommandError, run
from .git import LocalChanges, get_commit_subjects_since, get_local_changes
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


def create_changelist(message: str, base_branch: str, workspace_dir: str, dry_run: bool = False) -> str:
    """Create a new Perforce changelist with the given message and enumerated git commits.

    On dry run, returns the placeholder '<changelist>' so downstream
    commands can be rendered without a real changelist number."""
    # Build description: user message + enumerated commits
    commit_lines = get_enumerated_commit_lines_since(
        base_branch, workspace_dir)

    description_lines = message.splitlines()
    if commit_lines:
        description_lines += ['', 'Changes included:'] + commit_lines

    if dry_run:
        log.info("Would create new changelist with description:")
        log.info('\n'.join(description_lines))
        return '<changelist>'

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
    """Update the enumerated commit list in a changelist description.

    Commits in base_branch..HEAD replace their existing entries in the
    list (matched by subject) and new ones are appended; entries outside
    the range are kept. The whole list is renumbered. So `-b main`
    rebuilds the full list without duplicating it, while the review
    rebase flow (`-b HEAD~1` per picked commit) keeps appending."""
    # Fetch existing spec
    spec_text = get_changelist_spec(changelist_nr, workspace_dir)

    # Extract and split description into lines
    description_lines = extract_description_lines(spec_text)
    message_lines, old_commit_lines, trailing_lines = split_description_lines(
        description_lines)

    old_subjects = [re.sub(r'^\d+\. ', '', line)
                    for line in old_commit_lines]
    new_subjects = get_commit_subjects_since(base_branch, workspace_dir)

    # Drop old entries covered by the new range. Counted, not a set, so
    # repeated subjects (e.g. two "fixup" commits) replace one-for-one.
    replaced = Counter(new_subjects)
    kept_subjects = []
    for subject in old_subjects:
        if replaced[subject] > 0:
            replaced[subject] -= 1
        else:
            kept_subjects.append(subject)

    commit_lines = [f'{number}. {subject}' for number, subject
                    in enumerate(kept_subjects + new_subjects, 1)]

    # Rebuild description: message + commit list + trailing
    new_description_lines = message_lines + commit_lines + trailing_lines

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
