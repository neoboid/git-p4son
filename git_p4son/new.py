"""
New command implementation for git-p4son.

Creates a new Perforce changelist, opens files for edit, and optionally
creates a Swarm review.
"""

import argparse
import os
import sys
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
    workspace_dir = args.workspace_dir

    # Check alias availability before creating the changelist
    if args.alias and not args.dry_run:
        alias_path = os.path.join(
            workspace_dir, '.git-p4son', 'changelists', args.alias)
        if os.path.exists(alias_path) and not args.force:
            print(f'Alias "{args.alias}" already exists (use -f/--force to overwrite)',
                  file=sys.stderr)
            return 1

    # Create new changelist
    changelist = create_changelist(
        args.message, args.base_branch, workspace_dir, dry_run=args.dry_run)

    if not args.dry_run:
        print(f"Created changelist {changelist}")
        if args.alias:
            if not save_changelist_alias(args.alias, changelist,
                                         workspace_dir, force=args.force):
                return 1
            print(f'Saved alias "{args.alias}" -> {changelist}')

    # Open changed files for edit in the new changelist
    if not args.no_edit:
        open_changes_for_edit(
            changelist, args.base_branch, workspace_dir, args.dry_run)

    # Add #review keyword to changelist description
    if args.review:
        add_review_keyword_to_changelist(
            changelist, workspace_dir, dry_run=args.dry_run)

    # Shelve the changelist
    if args.shelve or args.review:
        p4_shelve_changelist(
            changelist, workspace_dir, dry_run=args.dry_run)

    return 0
