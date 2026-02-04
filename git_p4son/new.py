"""
New command implementation for git-p4son.

Creates a new Perforce changelist, opens files for edit, and optionally
creates a Swarm review.
"""

import argparse
import os
import sys
from .common import ensure_workspace
from .changelist_store import save_changelist_alias
from .lib import (
    create_changelist,
    open_changes_for_edit,
    add_review_keyword_to_changelist,
    p4_shelve_changelist,
)


def new_command(args: argparse.Namespace) -> int:
    """
    Execute the 'new' command.

    Creates a new changelist, optionally opens files for edit,
    and optionally creates a Swarm review.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = ensure_workspace()

    # Check alias availability before creating the changelist
    if args.alias and not args.dry_run:
        alias_path = os.path.join(
            workspace_dir, '.git-p4son', 'changelists', args.alias)
        if os.path.exists(alias_path) and not args.force:
            print(f'Alias "{args.alias}" already exists (use -f/--force to overwrite)',
                  file=sys.stderr)
            return 1

    # Create new changelist
    returncode, changelist = create_changelist(
        args.message, args.base_branch, workspace_dir, dry_run=args.dry_run)
    if returncode != 0:
        return returncode

    if not args.dry_run:
        print(f"Created changelist {changelist}")
        if args.alias:
            if not save_changelist_alias(args.alias, changelist,
                                         workspace_dir, force=args.force):
                return 1
            print(f'Saved alias "{args.alias}" -> {changelist}')

    # Open changed files for edit in the new changelist
    if not args.no_edit:
        returncode = open_changes_for_edit(
            changelist, args.base_branch, workspace_dir, args.dry_run)
        if returncode != 0:
            return returncode

    # Add #review keyword to changelist description
    if args.review:
        returncode = add_review_keyword_to_changelist(
            changelist, workspace_dir, dry_run=args.dry_run)
        if returncode != 0:
            return returncode

    # Shelve the changelist
    if args.shelve or args.review:
        returncode = p4_shelve_changelist(
            changelist, workspace_dir, dry_run=args.dry_run)
        if returncode != 0:
            return returncode

    return 0
