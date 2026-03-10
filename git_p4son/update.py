"""
Update command implementation for git-p4son.

Updates an existing Perforce changelist description, opens files for edit,
and optionally re-shelves.
"""

import argparse
from .changelist_store import load_changelist_alias
from .lib import update_changelist, open_changes_for_edit
from .perforce import p4_shelve_changelist
from .log import log


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

    # Update changelist description
    if not args.no_desc:
        log.heading(f'Updating description for CL {changelist}')
        update_changelist(
            changelist, args.base_branch, workspace_dir, dry_run=args.dry_run)
        log.success('Done')

    # Open changed files for edit
    if not args.no_edit:
        log.heading('Opening files for edit')
        open_changes_for_edit(
            changelist, args.base_branch, workspace_dir, args.dry_run)
        log.success('Done')

    # Shelve the changelist
    if args.shelve:
        log.heading('Shelving')
        p4_shelve_changelist(
            changelist, workspace_dir, dry_run=args.dry_run)
        log.success('Done')

    return 0
