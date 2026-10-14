"""New command: creates a Perforce changelist from the git change."""

import argparse
from .changelist_store import check_alias_usable, save_changelist_alias
from .lib import check_git_workspace_clean, create_changelist, edit_and_shelve
from .log import log


def new_command(args: argparse.Namespace) -> int:
    """Execute the new command."""
    workspace_dir = args.workspace_dir

    # Opening and reverting files relies on every tracked file matching HEAD.
    if not args.no_edit:
        check_git_workspace_clean(workspace_dir)

    if args.alias:
        check_alias_usable(args.alias, workspace_dir, args.force)

    log.heading('Creating changelist')
    changelist = create_changelist(
        args.message, args.base_branch, workspace_dir, dry_run=args.dry_run,
        commit_list=not args.no_commit_list, review=args.review)

    if not args.dry_run:
        if args.alias:
            save_changelist_alias(args.alias, changelist, workspace_dir,
                                  force=args.force)
            log.success(f'Created CL {changelist} (alias={args.alias})')
        else:
            log.success(f'Created CL {changelist}')

    edit_and_shelve(changelist, args.base_branch, workspace_dir,
                    edit=not args.no_edit, shelve=args.shelve or args.review,
                    dry_run=args.dry_run)
    return 0
