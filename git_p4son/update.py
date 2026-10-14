"""Update command: updates an existing changelist from the git change."""

import argparse
from .changelist_store import load_changelist_alias
from .lib import (
    check_git_workspace_clean,
    open_changes_and_revert_unchanged,
    update_changelist,
)
from .perforce import p4_shelve_changelist
from .log import log
from .rebase_todo import SWARM_SHELF_DELAY, rebase_per_commit


def _step_command(changelist: str, args: argparse.Namespace) -> str:
    """The update command each commit runs."""
    flags = ''.join(f' {flag}' for flag, on in (
        ('--no-commit-list', args.no_commit_list),
        ('--no-edit', args.no_edit),
        ('--shelve', args.shelve)) if on)
    return f'update {changelist}{flags}'


def _update_per_commit(changelist: str, args: argparse.Namespace) -> int:
    """Update the changelist once per commit since the base branch."""
    # Steps name the changelist by number: HEAD is detached during the rebase.
    cmd = _step_command(changelist, args)
    return rebase_per_commit(cmd, cmd, args.base_branch, args.workspace_dir,
                             message=args.message,
                             sleep=SWARM_SHELF_DELAY if args.shelve else None,
                             edit_todo=False, dry_run=args.dry_run)


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
