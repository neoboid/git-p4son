"""
Update command implementation for git-p4son.

Updates an existing Perforce changelist description, opens files for edit,
and optionally re-shelves. With --per-commit, does so once per commit since
the base branch, through an interactive rebase.
"""

import argparse
import shlex
from .changelist_store import load_changelist_alias
from .git import get_commit_lines_since
from .lib import (
    check_git_workspace_clean,
    open_changes_for_edit,
    revert_stale_files,
    update_changelist,
)
from .perforce import p4_shelve_changelist
from .log import log
from .rebase_todo import pick_line, run_todo_rebase


def _generate_todo(commit_lines: list[str], changelist: str,
                   args: argparse.Namespace) -> str:
    """Generate a rebase todo running update after each commit."""
    lines = []
    last_index = len(commit_lines) - 1
    for i, commit_line in enumerate(commit_lines):
        lines.append(pick_line(commit_line))

        cmd = f'update {changelist}'
        # The message only needs replacing once
        if i == 0 and args.message is not None:
            if args.file:
                cmd += f' -F {shlex.quote(args.file)}'
            else:
                cmd += f' -m {shlex.quote(args.message)}'
        if args.no_commit_list:
            cmd += ' --no-commit-list'
        if args.no_edit:
            cmd += ' --no-edit'
        if args.shelve:
            cmd += ' --shelve'

        # Sleep after all exec lines except the last, so Swarm can process
        # each shelf before the next
        if i < last_index:
            cmd += ' --sleep 5'
        lines.append(f'exec git p4son {cmd}')

    return '\n'.join(lines) + '\n'


def _update_per_commit(changelist: str, args: argparse.Namespace) -> int:
    """Update the changelist once per commit since the base branch."""
    workspace_dir = args.workspace_dir

    # The generated rebase todo is line-based, so an embedded newline in
    # the message would split the exec line and break the rebase. A message
    # read from a file is passed on by file name instead.
    if not args.file and args.message and '\n' in args.message:
        log.error('Message must be a single line with --per-commit '
                  '(use -F for a multi-line message)')
        return 1

    # The rebase needs a clean workspace even with --no-edit
    if not check_git_workspace_clean(workspace_dir):
        return 1

    log.heading('Finding commits')
    commit_lines = get_commit_lines_since(args.base_branch, workspace_dir)
    if not commit_lines:
        log.error(f'No commits found since {args.base_branch}')
        return 1
    log.success(f'{len(commit_lines)} commits since {args.base_branch}')

    # Each step names the changelist by number: HEAD is detached during the
    # rebase, so the current branch alias cannot be resolved there.
    log.heading('Generating rebase todo')
    todo_content = _generate_todo(commit_lines, changelist, args)

    if args.dry_run:
        log.info('Generated rebase todo:')
        log.info(todo_content)
        return 0

    return run_todo_rebase(todo_content, args.base_branch, workspace_dir,
                           edit_todo=False)


def update_command(args: argparse.Namespace) -> int:
    """Execute the update command."""
    workspace_dir = args.workspace_dir

    if args.changelist.isdigit():
        changelist = args.changelist
    else:
        log.heading('Resolving alias')
        changelist = load_changelist_alias(args.changelist, workspace_dir)
        if changelist is None:
            return 1
        log.success(f'{args.changelist} -> CL {changelist}')

    if args.per_commit:
        return _update_per_commit(changelist, args)

    # Opening and reverting files relies on every tracked file matching
    # HEAD, so refuse before touching the changelist. Also runs on dry
    # run, so it reports the same problem the real run would hit.
    if not args.no_edit and not check_git_workspace_clean(workspace_dir):
        return 1

    # Update changelist description, unless there is nothing to change
    if args.message is not None or not args.no_commit_list:
        log.heading(f'Updating description for CL {changelist}')
        update_changelist(
            changelist, args.base_branch, workspace_dir, dry_run=args.dry_run,
            message=args.message, commit_list=not args.no_commit_list)
        log.success('Done')

    # Open changed files for edit
    if not args.no_edit:
        log.heading('Opening files for edit')
        open_changes_for_edit(
            changelist, args.base_branch, workspace_dir, args.dry_run)
        log.success('Done')

        # Drop files that are no longer part of the git change, e.g. an
        # edit undone by a later commit, so the changelist and the shelf
        # only contain real changes.
        log.heading('Reverting unchanged files')
        count = revert_stale_files(changelist, workspace_dir, args.dry_run)
        log.success(f'{count} would be reverted' if args.dry_run
                    else f'{count} reverted')

    # Shelve the changelist
    if args.shelve:
        log.heading('Shelving')
        p4_shelve_changelist(
            changelist, workspace_dir, dry_run=args.dry_run)
        log.success('Done')

    return 0
