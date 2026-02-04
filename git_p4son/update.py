"""
Update command implementation for git-p4son.

Updates an existing Perforce changelist description, opens files for edit,
and optionally re-shelves.
"""

import argparse
from .common import ensure_workspace
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
    workspace_dir = ensure_workspace()

    changelist = resolve_changelist(args.changelist, workspace_dir)
    if changelist is None:
        return 1

    # Update changelist description
    returncode = update_changelist(
        changelist, args.base_branch, workspace_dir, dry_run=args.dry_run)
    if returncode != 0:
        return returncode

    if not args.dry_run:
        print(f"Updated changelist {changelist}")

    # Open changed files for edit
    if not args.no_edit:
        returncode = open_changes_for_edit(
            changelist, args.base_branch, workspace_dir, args.dry_run)
        if returncode != 0:
            return returncode

    # Shelve the changelist
    if args.shelve:
        returncode = p4_shelve_changelist(
            changelist, workspace_dir, dry_run=args.dry_run)
        if returncode != 0:
            return returncode

    return 0
