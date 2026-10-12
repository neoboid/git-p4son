"""Update command: updates an existing changelist from the git change."""

import argparse
import shlex
from .changelist_store import load_changelist_alias
from .git import get_commit_lines_since
from .lib import (
    check_git_workspace_clean,
    open_changes_and_revert_unchanged,
    update_changelist,
)
from .perforce import p4_shelve_changelist
from .log import log
from .rebase_todo import MESSAGE_FILE, build_todo, run_todo_rebase


def _generate_todo(commit_lines: list[str], changelist: str,
                   args: argparse.Namespace) -> str:
    """Generate a rebase todo running update after each commit."""
    flags = ''
    if args.no_commit_list:
        flags += ' --no-commit-list'
    if args.no_edit:
        flags += ' --no-edit'
    if args.shelve:
        flags += ' --shelve'
    first_cmd = f'update {changelist}'
    # The message only needs replacing once
    if args.message is not None:
        first_cmd += f' -F {shlex.quote(MESSAGE_FILE)}'
    commands = ([first_cmd + flags]
                + [f'update {changelist}{flags}'] * (len(commit_lines) - 1))
    return build_todo(commit_lines, commands)


def _update_per_commit(changelist: str, args: argparse.Namespace) -> int:
    """Update the changelist once per commit since the base branch."""
    workspace_dir = args.workspace_dir

    log.heading('Finding commits')
    commit_lines = get_commit_lines_since(args.base_branch, workspace_dir)
    if not commit_lines:
        log.error(f'No commits found since {args.base_branch}')
        return 1
    log.success(f'{len(commit_lines)} commits since {args.base_branch}')

    # Steps name the changelist by number: HEAD is detached during the rebase.
    log.heading('Generating rebase todo')
    todo_content = _generate_todo(commit_lines, changelist, args)

    return run_todo_rebase(todo_content, args.base_branch, workspace_dir,
                           edit_todo=False, message=args.message,
                           dry_run=args.dry_run)


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

    # Opening and reverting files relies on every tracked file matching HEAD.
    if not args.no_edit and not check_git_workspace_clean(workspace_dir):
        return 1

    # Update changelist description, unless there is nothing to change
    if args.message is not None or not args.no_commit_list:
        log.heading(f'Updating description for CL {changelist}')
        update_changelist(
            changelist, args.base_branch, workspace_dir, dry_run=args.dry_run,
            message=args.message, commit_list=not args.no_commit_list)
        log.success('Done')

    if not args.no_edit:
        open_changes_and_revert_unchanged(changelist, args.base_branch,
                                          workspace_dir, args.dry_run)

    if args.shelve:
        log.heading('Shelving')
        p4_shelve_changelist(
            changelist, workspace_dir, dry_run=args.dry_run)
        log.success('Done')

    return 0
