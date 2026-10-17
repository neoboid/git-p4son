"""Update command: updates an existing changelist from the git change."""

import argparse
from .changelist_store import load_changelist_alias
from .jj import change_revision
from .lib import check_workspace_clean, edit_and_shelve, update_changelist
from .log import log
from .per_commit import SWARM_SHELF_DELAY, run_per_commit


def _step(changelist: str, args: argparse.Namespace) -> list[str]:
    """The update step for every commit; run_per_commit gives the first one the message."""
    flags = [flag for flag, on in (
        ('--no-commit-list', args.no_commit_list),
        ('--no-edit', args.no_edit),
        ('--shelve', args.shelve)) if on]
    return ['update', changelist, *flags]


def _update_per_commit(changelist: str, args: argparse.Namespace) -> None:
    """Update the changelist once per commit since the base branch."""
    # Steps name the changelist by number, as resolved once here.
    run_per_commit(_step(changelist, args), None, args.base_branch, args.workspace_dir, args.backend,
                   sleep=SWARM_SHELF_DELAY if args.shelve else None, dry_run=args.dry_run,
                   message=args.message, message_file=args.file)


def update_command(args: argparse.Namespace) -> int:
    """Execute the update command."""
    workspace_dir = args.workspace_dir

    if args.changelist.isdigit():
        changelist = args.changelist
    else:
        log.heading('Resolving alias')
        changelist = load_changelist_alias(args.changelist, workspace_dir)
        log.success(f'{args.changelist} -> CL {changelist}')

    if args.per_commit:
        _update_per_commit(changelist, args)
        return 0

    revision = change_revision(args)

    # Opening and reverting files relies on every tracked file matching the change.
    if not args.no_edit:
        check_workspace_clean(workspace_dir, args.jj_workspace)

    if args.message is not None or not args.no_commit_list:
        log.heading(f'Updating description for CL {changelist}')
        update_changelist(
            changelist, args.base_branch, workspace_dir, dry_run=args.dry_run,
            message=args.message, commit_list=not args.no_commit_list,
            revision=revision)
        log.success(f'Would update CL {changelist} with this description'
                    if args.dry_run else f'Updated CL {changelist}')

    edit_and_shelve(changelist, args.base_branch, workspace_dir,
                    edit=not args.no_edit, shelve=args.shelve,
                    dry_run=args.dry_run, revision=revision)
    return 0
