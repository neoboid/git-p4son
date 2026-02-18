"""
Update command implementation for git-p4son.

Updates an existing Perforce changelist description, opens files for edit,
and optionally re-shelves.
"""

import argparse
from .changelist_store import resolve_changelist
from .lib import (
    update_changelist,
    open_changes_for_edit,
    p4_shelve_changelist,
)


def update_command(args: argparse.Namespace) -> int:
    """
    Execute the 'update' command.

    Updates an existing changelist description, optionally opens files
    for edit, and optionally re-shelves.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = args.workspace_dir

    changelist = resolve_changelist(args.changelist, workspace_dir)
    if changelist is None:
        return 1

    # Update changelist description
    update_changelist(
        changelist, args.base_branch, workspace_dir, dry_run=args.dry_run)

    if not args.dry_run:
        print(f"Updated changelist {changelist}")

    # Open changed files for edit
    if not args.no_edit:
        open_changes_for_edit(
            changelist, args.base_branch, workspace_dir, args.dry_run)

    # Shelve the changelist
    if args.shelve:
        p4_shelve_changelist(
            changelist, workspace_dir, dry_run=args.dry_run)

    return 0
