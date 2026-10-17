"""Bridge functions that combine git and Perforce operations."""

import contextlib
import os
import shutil
import stat
import tempfile
from typing import Callable, Iterator

from .common import CommandError
from .description import Description
from .git import (
    LocalChanges,
    get_commit_subjects_since,
    get_dirty_files,
    get_local_changes,
    get_tracked_files,
    restore_files,
)
from .log import log, plural
from .perforce import (
    extract_description_lines,
    get_changelist_spec,
    get_opened_files,
    get_opened_files_in_changelist,
    p4_create_changelist,
    p4_revert,
    p4_revert_unchanged,
    p4_shelve_changelist,
    opened_lookup,
    replace_description_in_spec,
    run_p4_batch,
    save_changelist_spec,
)
from .writable import is_writable_mode, restore_writable


DRY_RUN_CHANGELIST = '<changelist>'


def check_git_workspace_clean(workspace_dir: str) -> None:
    """Raise CommandError, after listing them, if the git workspace has uncommitted changes."""
    log.heading('Checking git workspace')
    dirty_files = get_dirty_files(workspace_dir)
    if dirty_files:
        for filename, change in dirty_files:
            log.file_change(filename, change)
        raise CommandError('Workspace is not clean. '
                           'Commit or stash the changes above first.')
    log.success('Clean')


def create_changelist(message: str, base_branch: str, workspace_dir: str, dry_run: bool,
                      commit_list: bool = True, review: bool = False,
                      revision: str | None = None) -> str:
    """Create a changelist from message and, unless commit_list is False, the commits since base_branch.

    With review, the description ends with #review unless the message has one on a line of its own.
    On dry run, creates nothing and returns DRY_RUN_CHANGELIST."""
    description = Description(
        commits=get_commit_subjects_since(base_branch, workspace_dir,
                                          revision or 'HEAD')
        if commit_list else [])
    description.set_message(message)
    if review and not description.keywords:
        description.add_keyword('#review')
    description_lines = description.format()

    if dry_run:
        log.info('\n'.join(description_lines))
        return DRY_RUN_CHANGELIST

    return p4_create_changelist(description_lines, workspace_dir)


def update_changelist(changelist_nr: str, base_branch: str, workspace_dir: str, dry_run: bool,
                      message: str | None, commit_list: bool,
                      revision: str | None = None) -> None:
    """Update a changelist description, merging the commits since base_branch into its commit list."""
    spec_text = get_changelist_spec(changelist_nr, workspace_dir)
    description = Description.parse(extract_description_lines(spec_text))
    if message is not None:
        description.set_message(message)
    if commit_list:
        description.merge_commits(
            get_commit_subjects_since(base_branch, workspace_dir,
                                      revision or 'HEAD'))
    new_description_lines = description.format()

    if dry_run:
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
    log.warning(
        f'p4 did not open {len(unopened)} of {plural(len(to_open), "file")}:')
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


@contextlib.contextmanager
def _restoring(filepaths: list[str], workspace_dir: str, dry_run: bool,
               keep_working_files: bool) -> Iterator[None]:
    """Put back the files a p4 revert in the block overwrites, from git or with keep_working_files as they were."""
    if not filepaths:
        yield
        return
    if not keep_working_files:
        yield
        restore_files(filepaths, workspace_dir, dry_run)
        return
    if dry_run:
        yield
        log.info(f'Would put back {plural(len(filepaths), "working file")}')
        return
    with tempfile.TemporaryDirectory(prefix='git-p4son-restore-') as directory:
        saved = []
        for index, filename in enumerate(filepaths):
            path = os.path.join(workspace_dir, filename)
            backup = os.path.join(directory, str(index))
            shutil.copy2(path, backup, follow_symlinks=False)
            saved.append((path, backup))
        try:
            yield
        finally:
            for path, backup in saved:
                if os.path.lexists(path):
                    if not os.path.islink(path):
                        os.chmod(path, os.stat(path).st_mode | stat.S_IWUSR)
                    os.unlink(path)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                shutil.copy2(backup, path, follow_symlinks=False)


def include_changes_in_changelist(changes: LocalChanges, changelist: str,
                                  workspace_dir: str, dry_run: bool,
                                  keep_working_files: bool = False) -> int:
    """Open local git changes for add/edit/delete in a Perforce changelist; returns how many it opened or moved.

    Files reopened with another action get git's version back, or with keep_working_files their working version."""
    wanted = ([(f, 'add') for f in changes.adds]
              + [(f, 'edit') for f in changes.mods]
              + [(f, 'delete') for f in changes.dels])
    for from_filename, to_filename in changes.moves:
        wanted += [(from_filename, 'delete'), (to_filename, 'add')]

    opened = opened_lookup(
        get_opened_files([f for f, _ in wanted], workspace_dir))
    to_open, to_reopen, to_revert, to_restore = _plan_opens(
        wanted, opened, changelist)

    with _restoring(to_restore, workspace_dir, dry_run, keep_working_files):
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
    if to_open and not dry_run:
        _warn_unopened(to_open, changelist, p4_output, workspace_dir)
    return len(to_open) + len(to_reopen)


def _changelist_name(changelist: str) -> str:
    """How output names changelist, which on a dry run of new does not exist yet."""
    return ('the new changelist' if changelist == DRY_RUN_CHANGELIST
            else f'CL {changelist}')


def edit_and_shelve(changelist: str, base_branch: str, workspace_dir: str, *,
                    edit: bool, shelve: bool, dry_run: bool,
                    revision: str | None = None) -> None:
    """Open the git changes and revert the files no longer part of them if edit, then shelve if shelve.

    A revision names the commit holding the change when git's HEAD and index do not, as in a jj workspace; the
    working files are then left as they are. create_changelist and update_changelist take it the same way."""
    if edit:
        log.heading('Opening files for edit')
        changes = get_local_changes(base_branch, workspace_dir,
                                    revision or 'HEAD')
        opened = include_changes_in_changelist(
            changes, changelist, workspace_dir, dry_run,
            keep_working_files=revision is not None)
        log.success(f'Would open {plural(opened, "file")}' if dry_run
                    else f'Opened {plural(opened, "file")}')

        revert_unchanged_files(changelist, workspace_dir, dry_run, revision)

    if shelve:
        name = _changelist_name(changelist)
        log.heading(f'Shelving {name}')
        p4_shelve_changelist(changelist, workspace_dir, dry_run=dry_run)
        log.success(f'Would shelve {name}' if dry_run else f'Shelved {name}')


def revert_unchanged_files(changelist: str, workspace_dir: str,
                           dry_run: bool, revision: str | None = None) -> None:
    """Revert files in a changelist that are no longer part of the git change, in the index or else in revision.

    Requires a clean git workspace."""
    log.heading('Reverting unchanged files')
    if changelist == DRY_RUN_CHANGELIST:
        log.success('Would revert the unchanged files in the new changelist')
        return

    opened = get_opened_files_in_changelist(changelist, workspace_dir)
    tracked = get_tracked_files([path for path, _ in opened], workspace_dir,
                                revision)

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

    # p4 revert writes the depot version to disk.
    with _restoring(stale_deletes, workspace_dir, dry_run,
                    keep_working_files=revision is not None):
        if stale_deletes:
            p4_revert(stale_deletes, workspace_dir, dry_run)

    count = len(reverted) + len(stale_deletes)
    log.success(f'{plural(count, "file")} would be reverted' if dry_run
                else f'{plural(count, "file")} reverted')

    if not dry_run and is_writable_mode(workspace_dir):
        log.heading('Making reverted tracked files writable (writable mode)')
        # p4 revert leaves files read-only; the still opened ones are writable already.
        changed = restore_writable(revert_if_unchanged + stale_deletes,
                                   tracked, workspace_dir)
        log.success(f'{plural(changed, "tracked file")} made writable')
