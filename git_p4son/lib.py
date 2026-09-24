"""
Bridge functions that combine git and Perforce operations.
"""

import os
import re
from collections import Counter

from .common import CommandError, run
from .git import (
    LocalChanges,
    get_commit_subjects_since,
    get_dirty_files,
    get_local_changes,
    get_tracked_files,
)
from .list_changes import get_enumerated_commit_lines_since
from .log import log
from .perforce import (
    extract_description_lines,
    get_changelist_spec,
    get_opened_files_in_changelist,
    include_changes_in_changelist,
    p4_revert_unchanged,
    replace_description_in_spec,
)
from .writable import is_writable_mode, make_writable


# Heading written above the enumerated commit list in descriptions.
COMMIT_LIST_MARKER = 'Changes included:'


def split_description_lines(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Split description into (message_lines, commit_lines, trailing_lines).

    The commit list is located via the COMMIT_LIST_MARKER heading written
    above it, so a numbered list inside the user's own message is not
    mistaken for it. Descriptions without the marker fall back to the
    first '1. ' line."""
    start = None
    for i, line in enumerate(lines):
        if line.strip() != COMMIT_LIST_MARKER:
            continue
        for j in range(i + 1, len(lines)):
            if lines[j].startswith('1. '):
                start = j
                break
            if lines[j].strip():
                break  # something other than the list follows this marker
        if start is not None:
            break

    if start is None:
        # Fallback for descriptions written without the marker.
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


def check_git_workspace_clean(workspace_dir: str) -> bool:
    """Report whether the git workspace has no uncommitted changes."""
    log.heading('Checking git workspace')
    dirty_files = get_dirty_files(workspace_dir)
    if dirty_files:
        for filename, change in dirty_files:
            log.file_change(filename, change)
        log.error('Workspace is not clean')
        return False
    log.success('clean')
    return True


def create_changelist(message: str, base_branch: str, workspace_dir: str, dry_run: bool = False) -> str:
    """Create a new Perforce changelist with the given message and enumerated git commits.

    On dry run, returns the placeholder '<changelist>' so downstream
    commands can be rendered without a real changelist number."""
    # Build description: user message + enumerated commits
    commit_lines = get_enumerated_commit_lines_since(
        base_branch, workspace_dir)

    description_lines = message.splitlines()
    if commit_lines:
        description_lines += ['', COMMIT_LIST_MARKER] + commit_lines

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

    # Add the marker for descriptions that did not have it yet (e.g.
    # created with no commits), so later splits anchor on it.
    if commit_lines and not any(line.strip() == COMMIT_LIST_MARKER
                                for line in message_lines):
        message_lines = message_lines + ['', COMMIT_LIST_MARKER]

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


def revert_stale_files(changelist: str, workspace_dir: str,
                       dry_run: bool = False) -> int:
    """Revert files in a changelist that are no longer part of the git change.

    Only files git-p4son has authority over are considered: files tracked
    by git, whose content git holds, and files opened for add that are
    missing from disk, where there is nothing to lose. Other files, like
    p4-only binaries opened by hand, are left alone even when unchanged.
    Requires a clean git workspace, so tracked files on disk match HEAD.
    Returns the number of files reverted (or that would be on dry run)."""
    if dry_run and not changelist.isdigit():
        # Placeholder from a dry-run new: there is no changelist to query.
        log.info(f'Would revert unchanged files in changelist {changelist}')
        return 0

    opened = get_opened_files_in_changelist(changelist, workspace_dir)
    tracked = get_tracked_files([path for path, _ in opened], workspace_dir)

    # Unchanged edits and missing adds: revert -a picks out the ones that
    # qualify, so changed files stay opened.
    revert_if_unchanged = []
    # Opened for delete although git has the file: deleted in one commit
    # and restored in a later one.
    stale_deletes = []
    for path, action in opened:
        exists = os.path.lexists(os.path.join(workspace_dir, path))
        if action == 'edit' and path in tracked:
            revert_if_unchanged.append(path)
        elif action == 'add' and not exists:
            revert_if_unchanged.append(path)
        elif action == 'delete' and path in tracked and exists:
            stale_deletes.append(path)

    reverted = p4_revert_unchanged(revert_if_unchanged, changelist,
                                   workspace_dir, dry_run)
    for line in reverted:
        log.info(line)

    for path in stale_deletes:
        # p4 revert writes the depot version to disk, so restore git's.
        run(['p4', 'revert', path], cwd=workspace_dir, dry_run=dry_run)
        run(['git', 'restore', path], cwd=workspace_dir, dry_run=dry_run)

    if not dry_run and is_writable_mode(workspace_dir):
        # p4 revert leaves files read-only. Files still opened are already
        # writable, so there is no need to pick out the reverted ones.
        make_writable([os.path.join(workspace_dir, path)
                       for path in revert_if_unchanged + stale_deletes
                       if path in tracked])

    return len(reverted) + len(stale_deletes)
