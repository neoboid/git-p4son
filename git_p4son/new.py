"""New command: creates a Perforce changelist from the git change."""

import argparse
from .changelist_store import (
    alias_exists,
    save_changelist_alias,
    validate_alias_name,
)
from .lib import (
    check_git_workspace_clean,
    create_changelist,
    open_changes_for_edit,
    revert_stale_files,
)
from .perforce import add_review_keyword_to_changelist, p4_shelve_changelist
from .log import log


def new_command(args: argparse.Namespace) -> int:
    """Execute the new command."""
    workspace_dir = args.workspace_dir

    # Opening and reverting files relies on every tracked file matching HEAD.
    if not args.no_edit and not check_git_workspace_clean(workspace_dir):
        return 1

    # Validate the alias before creating the changelist.
    if args.alias:
        error = validate_alias_name(args.alias)
        if error:
            log.error(error)
            return 1
        if alias_exists(args.alias, workspace_dir) and not args.force:
            log.error(
                f'Alias "{args.alias}" already exists '
                f'(use -f/--force to overwrite)')
            return 1

    log.heading('Creating changelist')
    changelist = create_changelist(
        args.message, args.base_branch, workspace_dir, dry_run=args.dry_run,
        commit_list=not args.no_commit_list)

    if not args.dry_run:
        if args.alias:
            if not save_changelist_alias(args.alias, changelist,
                                         workspace_dir, force=args.force):
                return 1
            log.success(f'Created CL {changelist} (alias={args.alias})')
        else:
            log.success(f'Created CL {changelist}')

    if not args.no_edit:
        log.heading('Opening files for edit')
        open_changes_for_edit(
            changelist, args.base_branch, workspace_dir, args.dry_run)
        log.success('Done')

        log.heading('Reverting unchanged files')
        count = revert_stale_files(changelist, workspace_dir, args.dry_run)
        log.success(f'{count} would be reverted' if args.dry_run
                    else f'{count} reverted')

    if args.review:
        log.heading('Adding review keyword')
        add_review_keyword_to_changelist(
            changelist, workspace_dir, dry_run=args.dry_run)
        log.success('Done')

    if args.shelve or args.review:
        log.heading('Shelving')
        p4_shelve_changelist(
            changelist, workspace_dir, dry_run=args.dry_run)
        log.success('Done')

    return 0
