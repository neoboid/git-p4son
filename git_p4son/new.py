"""
New command implementation for git-p4son.

Creates a new Perforce changelist, opens files for edit, and optionally
creates a Swarm review.
"""

import argparse
from .changelist_store import alias_exists, save_changelist_alias
from .lib import (
    create_changelist,
    open_changes_for_edit,
    add_review_keyword_to_changelist,
    p4_shelve_changelist,
)
from .log import log


def new_command(args: argparse.Namespace) -> int:
    """Execute the new command."""
    workspace_dir = args.workspace_dir

    # Check alias availability before creating the changelist
    if args.alias and not args.dry_run:
        if alias_exists(args.alias, workspace_dir) and not args.force:
            log.error(
                f'Alias "{args.alias}" already exists '
                f'(use -f/--force to overwrite)')
            return 1

    # Create new changelist
    log.heading('Creating changelist')
    changelist = create_changelist(
        args.message, args.base_branch, workspace_dir, dry_run=args.dry_run)

    if not args.dry_run:
        if args.alias:
            if not save_changelist_alias(args.alias, changelist,
                                         workspace_dir, force=args.force):
                return 1
            log.success(f'Created CL {changelist} (alias={args.alias})')
        else:
            log.success(f'Created CL {changelist}')

    # Open changed files for edit in the new changelist
    if not args.no_edit:
        log.heading('Opening files for edit')
        open_changes_for_edit(
            changelist, args.base_branch, workspace_dir, args.dry_run)
        log.success('Done')

    # Add #review keyword to changelist description
    if args.review:
        log.heading('Adding review keyword')
        add_review_keyword_to_changelist(
            changelist, workspace_dir, dry_run=args.dry_run)
        log.success('Done')

    # Shelve the changelist
    if args.shelve or args.review:
        log.heading('Shelving')
        p4_shelve_changelist(
            changelist, workspace_dir, dry_run=args.dry_run)
        log.success('Done')

    return 0
