"""Bridge functions that combine git and Perforce operations."""

import argparse
import os
from typing import Callable, NamedTuple

from .common import CommandError
from .description import Description
from .git import (
    LocalChanges,
    get_commit_subjects_since,
    get_dirty_files,
    get_local_changes,
    get_tracked_files,
)
from .log import log, plural
from .perforce import (
    extract_description_lines,
    get_changelist_spec,
    get_opened_files,
    get_opened_files_and_moves,
    get_opened_files_in_changelist,
    get_out_of_date_files,
    OutOfDate,
    p4_create_changelist,
    p4_revert_unchanged,
    p4_shelve_changelist,
    opened_lookup,
    replace_description_in_spec,
    p4_move,
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
                      revision: str = 'HEAD') -> str:
    """Create a changelist from message and, unless commit_list is False, the commits since base_branch.

    With review, the description ends with #review unless the message has one on a line of its own.
    On dry run, creates nothing and returns DRY_RUN_CHANGELIST."""
    description = Description(
        commits=get_commit_subjects_since(base_branch, workspace_dir, revision)
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
                      revision: str = 'HEAD', new_commits: list[str] | None = None) -> None:
    """Update a changelist description, merging the commits since base_branch into its commit list.

    With new_commits, appends those instead, leaving the entries the list has as they are, and skipping the leading
    ones the list already ends with."""
    spec_text = get_changelist_spec(changelist_nr, workspace_dir)
    description = Description.parse(extract_description_lines(spec_text))
    if message is not None:
        description.set_message(message)
    if commit_list and new_commits is not None:
        # A retry after a failed open or shelve finds the first ones appended, as the record only advances after those.
        appended = next((count for count in range(len(new_commits), 0, -1)
                         if description.commits[-count:] == new_commits[:count]), 0)
        description.commits += new_commits[appended:]
    elif commit_list:
        description.merge_commits(
            get_commit_subjects_since(base_branch, workspace_dir, revision))
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
                changelist: str) -> tuple[list[tuple[str, str]], list[str], list[str]]:
    """Sort (file, action) pairs into (to_open, to_reopen, to_revert) given their opened state."""
    to_open = []
    to_reopen = []
    to_revert = []
    for filename, p4_action in wanted:
        current = opened(filename)
        if current is None:
            to_open.append((filename, p4_action))
            continue
        current_cl, current_action = current
        # add -> edit: new to the depot, so it stays an add. A move's new path stays one through later edits.
        if current_action == p4_action or (current_action in ('add', 'move/add')
                                           and p4_action == 'edit'):
            if current_cl != changelist:
                to_reopen.append(filename)
            continue
        # Revert and reopen with the right action.
        to_revert.append(filename)
        # add -> delete: never in the depot, so just revert.
        if current_action == 'add' and p4_action == 'delete':
            continue
        to_open.append((filename, p4_action))
    return to_open, to_reopen, to_revert


def _revisions(file: OutOfDate) -> str:
    """How far behind the head revision file is, for listing it."""
    if file.have is None:
        return f'not synced, head #{file.head}'
    if file.deleted_at_head:
        return f'have #{file.have}, deleted at head #{file.head}'
    return f'have #{file.have}, head #{file.head}'


def check_at_latest_revision(args: argparse.Namespace, base_branch: str | None, changelist: str | None,
                             revision: str) -> None:
    """Raise CommandError, after listing them, if files opened in changelist, or of the change since base_branch when
    given, aren't at the latest revision; --ignore-out-of-date and per-commit steps skip the check."""
    # The steps of a per-commit run, or of the shelves at an update's catch-up, leave it to the run's own check.
    if args.ignore_out_of_date or args.per_commit_step:
        return
    workspace_dir = args.workspace_dir
    log.heading('Checking files are at the latest revision')
    files = []
    if base_branch is not None:
        changes = get_local_changes(base_branch, workspace_dir, revision)
        files += [*changes.adds, *changes.mods, *changes.dels,
                  *(path for move in changes.moves for path in move)]
    if changelist is not None:
        files += [path for path,
                  _ in get_opened_files_in_changelist(changelist, workspace_dir)]
    files = list(dict.fromkeys(files))
    stale = get_out_of_date_files(files, workspace_dir)
    if stale:
        for file in stale:
            log.info(f'{file.path} ({_revisions(file)})')
        raise CommandError('Files in the change are not at the latest revision. Run "git p4son sync", rebase the '
                           'change onto it and try again, or pass --ignore-out-of-date to shelve anyway.')
    log.success(f'{plural(len(files), "file")} at the latest revision')


class _Move(NamedTuple):
    """A rename to open with p4 move, after opening old for edit unless edit is False."""
    old: str
    new: str
    # False for the target of an earlier move in the CL, which p4 moves on from where it started.
    edit: bool


def _plan_moves(moves: list[tuple[str, str]], changelist: str, opened: Callable[[str], tuple[str, str] | None],
                other_end: dict[str, str]) -> tuple[list[_Move], list[str], list[tuple[str, str]]]:
    """Sort git's renames into (to_move, to_unmove, wanted) given what is opened and each opened move's other end.

    to_unmove are the files to revert, keeping the working files, before opening anything; wanted are the (file, action)
    pairs left to _plan_opens: a move already open, to keep or reopen, the delete and add of a file new to the depot,
    and an edit where the rename doesn't move the file."""
    to_move = []
    to_unmove = []
    wanted = []
    for old, new in moves:
        old_cl, old_action = opened(old) or (None, None)
        if old_action == 'add':
            # Added earlier in the change, so not in the depot: nothing to move.
            wanted += [(old, 'delete'), (new, 'add')]
        elif old.casefold() == new.casefold():
            # The same file on a case-insensitive server, which p4 can't move onto.
            wanted.append((old, 'edit'))
        elif other_end.get(old) == new and old_action == 'move/delete':
            wanted += [(old, 'move/delete'), (new, 'move/add')]
        elif other_end.get(old) == new:
            # Moved back to where it started: reverting the move puts it there, with any edit it kept.
            to_unmove.append(old)
            wanted.append((new, 'edit'))
        else:
            chained = (old_cl, old_action) == (changelist, 'move/add')
            if old_action is not None and not chained:
                to_unmove.append(old)
            if opened(new) is not None:
                to_unmove.append(new)
            to_move.append(_Move(old, new, edit=not chained))
    return to_move, to_unmove, wanted


def include_changes_in_changelist(changes: LocalChanges, changelist: str,
                                  workspace_dir: str, dry_run: bool) -> int:
    """Open local git changes for add/edit/delete, and renames for move, in a Perforce changelist; returns how many
    it opened, moved or reopened.

    Files reopened with another action keep their working version."""
    wanted = ([(f, 'add') for f in changes.adds]
              + [(f, 'edit') for f in changes.mods]
              + [(f, 'delete') for f in changes.dels])
    paths = [f for f, _ in wanted] + \
        [path for move in changes.moves for path in move]
    states, other_end = get_opened_files_and_moves(paths, workspace_dir)
    to_move, to_unmove, move_wanted = _plan_moves(
        changes.moves, changelist, opened_lookup(states), other_end)
    wanted += move_wanted
    # Reverting one end of a move reverts both, which the plan below has to see.
    for path in to_unmove:
        states.pop(path, None)
        states.pop(other_end.get(path, ''), None)
    to_open, to_reopen, to_revert = _plan_opens(
        wanted, opened_lookup(states), changelist)

    # -k: p4 writes nothing to disk, so each working file keeps git's version.
    to_revert = to_unmove + to_revert
    # p4 refuses to revert a move/delete without its move/add.
    to_revert += [other_end[path] for path in to_revert
                  if other_end.get(path, path) not in to_revert]
    run_p4_batch(['revert', '-k'], to_revert, 'Reverting files opened with another action',
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
    p4_output += p4_move([(move.old, move.new) for move in to_move],
                         [move.old for move in to_move if move.edit], changelist, workspace_dir, dry_run)
    expected = to_open + [(move.new, 'move/add') for move in to_move]
    if expected and not dry_run:
        _warn_unopened(expected, changelist, p4_output, workspace_dir)
    return len(to_open) + len(to_reopen) + len(to_move)


def _changelist_name(changelist: str) -> str:
    """How output names changelist, which on a dry run of new does not exist yet."""
    return ('the new changelist' if changelist == DRY_RUN_CHANGELIST
            else f'CL {changelist}')


def edit_and_shelve(changelist: str, base_branch: str, workspace_dir: str, *,
                    edit: bool, shelve: bool, dry_run: bool,
                    revision: str = 'HEAD') -> None:
    """Open the changes in revision since base_branch and revert the files no longer part of them if edit, then shelve.

    The working files are left as they are."""
    if edit:
        log.heading('Opening files for edit')
        changes = get_local_changes(base_branch, workspace_dir, revision)
        opened = include_changes_in_changelist(
            changes, changelist, workspace_dir, dry_run)
        log.success(f'Would open {plural(opened, "file")}' if dry_run
                    else f'Opened {plural(opened, "file")}')

        revert_unchanged_files(changelist, workspace_dir, dry_run, revision)

    if shelve:
        name = _changelist_name(changelist)
        log.heading(f'Shelving {name}')
        p4_shelve_changelist(changelist, workspace_dir, dry_run=dry_run)
        log.success(f'Would shelve {name}' if dry_run else f'Shelved {name}')


def revert_unchanged_files(changelist: str, workspace_dir: str,
                           dry_run: bool, revision: str = 'HEAD') -> None:
    """Revert files in a changelist that are no longer part of the change in revision.

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
    # Moves whose new path is gone, as the rename was undone; reverting one end reverts both.
    stale_moves = []
    for path, action in opened:
        exists = os.path.lexists(os.path.join(workspace_dir, path))
        if action == 'edit' and path in tracked:
            revert_if_unchanged.append(path)
        elif action == 'add' and not exists:
            revert_if_unchanged.append(path)
        elif action == 'delete' and path in tracked and exists:
            stale_deletes.append(path)
        elif action == 'move/add' and path not in tracked and not exists:
            stale_moves.append(path)

    reverted = p4_revert_unchanged(revert_if_unchanged, changelist,
                                   workspace_dir, dry_run)
    for line in reverted:
        log.info(line)

    # -k: git has the deleted files back, and the old paths of undone moves.
    run_p4_batch(['revert', '-k'], stale_deletes + stale_moves,
                 'Reverting deletes and moves no longer in the change', workspace_dir, dry_run)

    count = len(reverted) + len(stale_deletes) + len(stale_moves)
    log.success(f'{plural(count, "file")} would be reverted' if dry_run
                else f'{plural(count, "file")} reverted')

    if count and not dry_run and is_writable_mode(workspace_dir):
        log.heading('Making reverted tracked files writable (writable mode)')
        # p4 revert leaves files read-only; the still opened ones are writable already.
        changed = restore_writable(revert_if_unchanged + stale_deletes,
                                   tracked, workspace_dir)
        log.success(f'{plural(changed, "tracked file")} made writable')
