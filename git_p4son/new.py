"""New command: creates a Perforce changelist from the git change."""

import argparse
from .changelist_store import alias_for_change, check_alias_usable, load_changelist_alias, save_changelist_alias
from .common import CommandError
from .git import resolve_commit
from .included import new_record
from .lib import create_changelist, edit_and_shelve
from .log import log


def new_command(args: argparse.Namespace) -> int:
    """Execute the new command."""
    workspace_dir = args.workspace_dir
    revision = args.backend.revision

    # Opening and reverting files relies on every tracked file matching the change.
    if not args.no_edit:
        args.backend.check_clean()

    change = args.backend.change_id()
    base = None
    if args.alias:
        check_alias_usable(args.alias, workspace_dir, args.force)
        _check_one_alias_per_change(
            args.alias, change, workspace_dir, args.force)
        base = args.backend.base_name(args.base_given)

    log.heading('Creating changelist')
    changelist = create_changelist(
        args.message, args.base_branch, workspace_dir, dry_run=args.dry_run,
        commit_list=not args.no_commit_list, review=args.review,
        revision=revision)

    alias_note = f' (alias={args.alias})' if args.alias else ''
    if args.dry_run:
        log.success(
            f'Would create a changelist with this description{alias_note}')
    else:
        if args.alias and base is not None:
            record = new_record(base, resolve_commit(args.base_branch, workspace_dir), revision,
                                args.backend, workspace_dir)
            save_changelist_alias(args.alias, changelist, workspace_dir,
                                  force=args.force, change=change, record=record)
        log.success(f'Created CL {changelist}{alias_note}')

    edit_and_shelve(changelist, args.base_branch, workspace_dir,
                    edit=not args.no_edit, shelve=args.shelve or args.review,
                    dry_run=args.dry_run, revision=revision)
    return 0


def _check_one_alias_per_change(alias: str, change: str | None, workspace_dir: str, force: bool) -> None:
    """Raise CommandError if another alias records change, unless force moves the record to alias."""
    name = None if change is None else alias_for_change(change, workspace_dir)
    if name is None or name == alias or force:
        return
    changelist = load_changelist_alias(name, workspace_dir)
    raise CommandError(f'Alias "{name}" already records this jj change, for CL {changelist}. Run '
                       f'"git p4son update {name}" to update it, or pass --force to move the record to "{alias}".')
