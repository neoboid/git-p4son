"""Bridge functions that combine git and Perforce operations."""

import os
from typing import Callable

from .description import COMMIT_LIST_MARKER, Description
from .git import (
    LocalChanges,
    get_commit_subjects_since,
    get_dirty_files,
    get_local_changes,
    get_tracked_files,
    restore_files,
)
from .list_changes import get_enumerated_commit_lines_since
from .log import log
from .perforce import (
    extract_description_lines,
    get_changelist_spec,
    get_opened_files,
    get_opened_files_in_changelist,
    p4_create_changelist,
    p4_revert,
    p4_revert_unchanged,
    opened_lookup,
    replace_description_in_spec,
    run_p4_batch,
    save_changelist_spec,
)
from .writable import is_writable_mode, make_writable


DRY_RUN_CHANGELIST = '<changelist>'


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


def create_changelist(message: str, base_branch: str, workspace_dir: str, dry_run: bool = False,
                      commit_list: bool = True, review: bool = False) -> str:
    """Create a changelist from message and, unless commit_list is False, the git commits.

    With review, the description ends with #review unless the message has one. On dry run, creates
    nothing and returns DRY_RUN_CHANGELIST."""
    commit_lines = (get_enumerated_commit_lines_since(base_branch, workspace_dir)
                    if commit_list else [])

    description_lines = message.splitlines()
    if commit_lines:
        description_lines += ['', COMMIT_LIST_MARKER] + commit_lines
    if review and not any('#review' in line for line in description_lines):
        description_lines += ['', '#review']

    if dry_run:
        log.info("Would create new changelist with description:")
        log.info('\n'.join(description_lines))
        return DRY_RUN_CHANGELIST

    return p4_create_changelist(description_lines, workspace_dir)


def update_changelist(changelist_nr: str, base_branch: str, workspace_dir: str, dry_run: bool = False,
                      message: str | None = None, commit_list: bool = True) -> None:
    """Update a changelist description, merging commits since base_branch into its commit list."""
    spec_text = get_changelist_spec(changelist_nr, workspace_dir)
    description = Description.parse(extract_description_lines(spec_text))
    if message is not None:
        description.set_message(message)
    if commit_list:
        description.merge_commits(
            get_commit_subjects_since(base_branch, workspace_dir))
    new_description_lines = description.format()

    if dry_run:
        log.info(f"Would update changelist {changelist_nr} with description:")
        log.info('\n'.join(new_description_lines))
        return

    save_changelist_spec(
        replace_description_in_spec(spec_text, new_description_lines),
        workspace_dir)


def _warn_unopened(to_open: list[tuple[str, str]], changelist: str,
                   p4_output: list[str], workspace_dir: str) -> None:
    """Warn about the files p4 did not open as asked; p4 still exits 0 for most of them."""
    opened = opened_lookup(
        get_opened_files([f for f, _ in to_open], workspace_dir))
    unopened = [(f, action) for f, action in to_open
                if opened(f) != (changelist, action)]
    if not unopened:
        return
    log.warning(f'p4 did not open {len(unopened)} of {len(to_open)} files:')
    for filename, action in unopened:
        log.info(f'  {filename} (for {action})')
    messages = [line for line in p4_output if 'opened for' not in line]
    if messages:
        log.info('p4 said:')
        for line in messages:
            log.info(f'  {line}')


def _plan_opens(wanted: list[tuple[str, str]], opened: Callable[[str], tuple[str, str] | None],
                changelist: str) -> tuple[list[tuple[str, str]], list[str], list[str], list[str]]:
    """Sort (file, action) pairs into (to_open, to_reopen, to_revert, to_restore) given their opened state."""
    to_open = []
    to_reopen = []
    to_revert = []
    to_restore = []
    for filename, p4_action in wanted:
        current = opened(filename)
        if current is None:
            to_open.append((filename, p4_action))
            continue
        current_cl, current_action = current
        # add -> edit: new to the depot, so it stays an add.
        if current_action == p4_action or (current_action == 'add'
                                           and p4_action == 'edit'):
            if current_cl != changelist:
                to_reopen.append(filename)
            continue
        # Revert and reopen with the right action, restoring git's content p4 revert overwrote.
        to_revert.append(filename)
        # add -> delete: never in the depot, so just revert.
        if current_action == 'add' and p4_action == 'delete':
            continue
        to_open.append((filename, p4_action))
        if p4_action != 'delete':
            to_restore.append(filename)
    return to_open, to_reopen, to_revert, to_restore


def include_changes_in_changelist(changes: LocalChanges, changelist: str,
                                  workspace_dir: str, dry_run: bool = False) -> None:
    """Open local git changes for add/edit/delete in a Perforce changelist."""
    wanted = ([(f, 'add') for f in changes.adds]
              + [(f, 'edit') for f in changes.mods]
              + [(f, 'delete') for f in changes.dels])
    for from_filename, to_filename in changes.moves:
        wanted += [(from_filename, 'delete'), (to_filename, 'add')]

    opened = opened_lookup(
        get_opened_files([f for f, _ in wanted], workspace_dir))
    to_open, to_reopen, to_revert, to_restore = _plan_opens(
        wanted, opened, changelist)

    run_p4_batch(['revert'], to_revert, 'Reverting files opened with another action',
                 workspace_dir, dry_run)
    p4_output = []
    for p4_action in ('add', 'edit', 'delete'):
        p4_output += run_p4_batch(
            [p4_action, '-c', changelist],
            [f for f, action in to_open if action == p4_action],
            f'Opening for {p4_action}', workspace_dir, dry_run,
            fail_on_returncode=False)
    run_p4_batch(['reopen', '-c', changelist], to_reopen,
                 'Moving files from other changelists', workspace_dir, dry_run)
    if to_restore:
        restore_files(to_restore, workspace_dir, dry_run)
    if to_open and not dry_run:
        _warn_unopened(to_open, changelist, p4_output, workspace_dir)


def open_changes_for_edit(changelist: str, base_branch: str, workspace_dir: str, dry_run: bool = False) -> None:
    """Get local git changes and open them for edit in a Perforce changelist."""
    changes = get_local_changes(base_branch, workspace_dir)
    include_changes_in_changelist(changes, changelist, workspace_dir, dry_run)


def open_changes_and_revert_unchanged(changelist: str, base_branch: str,
                                      workspace_dir: str, dry_run: bool) -> None:
    """Open the git changes in the changelist, then revert the files no longer part of them."""
    log.heading('Opening files for edit')
    open_changes_for_edit(changelist, base_branch, workspace_dir, dry_run)
    log.success('Done')

    log.heading('Reverting unchanged files')
    count = revert_unchanged_files(changelist, workspace_dir, dry_run)
    log.success(f'{count} would be reverted' if dry_run
                else f'{count} reverted')


def revert_unchanged_files(changelist: str, workspace_dir: str,
                           dry_run: bool = False) -> int:
    """Revert files in a changelist that are no longer part of the git change.

    Requires a clean git workspace. Returns the number of files reverted."""
    if changelist == DRY_RUN_CHANGELIST:
        log.info(f'Would revert unchanged files in changelist {changelist}')
        return 0

    opened = get_opened_files_in_changelist(changelist, workspace_dir)
    tracked = get_tracked_files([path for path, _ in opened], workspace_dir)

    # revert -a only reverts the unchanged ones, so changed files stay opened.
    revert_if_unchanged = []
    # Deleted in one commit and restored in a later one.
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

    if stale_deletes:
        # p4 revert writes the depot version to disk, so restore git's.
        p4_revert(stale_deletes, workspace_dir, dry_run)
        restore_files(stale_deletes, workspace_dir, dry_run)

    if not dry_run and is_writable_mode(workspace_dir):
        # p4 revert leaves files read-only; the still opened ones are writable already.
        make_writable([os.path.join(workspace_dir, path)
                       for path in revert_if_unchanged + stale_deletes
                       if path in tracked])

    return len(reverted) + len(stale_deletes)
