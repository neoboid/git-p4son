"""New command: creates a Perforce changelist from the git change, at once or commit by commit."""

import argparse
from dataclasses import replace
from .changelist_store import (
    alias_for_change,
    check_alias_usable,
    list_changelist_aliases,
    load_changelist_alias,
    save_changelist_alias,
    update_record,
)
from .common import CommandError
from .git import resolve_commit
from .included import new_record
from .lib import create_changelist, edit_and_shelve
from .log import log
from .per_commit import SWARM_SHELF_DELAY, run_per_commit


def new_command(args: argparse.Namespace) -> int:
    """Execute the new command."""
    if args.per_commit:
        _new_per_commit(args)
        return 0
    workspace_dir = args.workspace_dir
    revision = args.backend.revision

    change = args.backend.change_id()
    if args.alias:
        check_alias_usable(args.alias, workspace_dir, args.force)
        _check_one_alias_per_change(
            args.alias, change, workspace_dir, args.force)

    log.heading('Creating changelist')
    base = args.backend.base_name(args.base_given) if args.alias else None
    changelist = create_changelist(
        args.message, args.base_branch, workspace_dir, dry_run=args.dry_run,
        commit_list=not args.no_commit_list, review=args.review,
        revision=revision)

    alias_note = f' (alias={args.alias})' if args.alias else ''
    record = None
    if args.dry_run:
        log.success(
            f'Would create a changelist with this description{alias_note}')
    else:
        if args.alias and base is not None:
            record = new_record(base, resolve_commit(args.base_branch, workspace_dir), revision,
                                args.shelve or args.review, args.backend, workspace_dir)
            save_changelist_alias(args.alias, changelist, workspace_dir, force=args.force, change=change,
                                  record=replace(record, base_commit='', included=[]))
        log.success(f'Created CL {changelist}{alias_note}')

    edit_and_shelve(changelist, args.base_branch, workspace_dir,
                    edit=not args.no_edit, shelve=args.shelve or args.review,
                    dry_run=args.dry_run, revision=revision)
    if record is not None:
        update_record(args.alias, record, workspace_dir)
    return 0


def _check_one_alias_per_change(alias: str, change: str | None, workspace_dir: str, force: bool) -> None:
    """Raise CommandError if another alias records change, unless force moves the record to alias."""
    name = None if change is None else alias_for_change(change, workspace_dir)
    if name is None or name == alias or force:
        return
    changelist = load_changelist_alias(name, workspace_dir)
    raise CommandError(f'Alias "{name}" already records this jj change, for CL {changelist}. Run '
                       f'"git p4son update {name}" to update it, or pass --force to move the record to "{alias}".')


def _steps(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """The new step for the first commit and the update step for the others, both shelving if new would."""
    shelve = ['--review'] if args.review else ['--shelve'] if args.shelve else []
    commit_list = ['--no-commit-list'] if args.no_commit_list else []
    force = ['--force'] if args.force else []
    return (['new', args.alias, *shelve, *force, *commit_list],
            ['update', args.alias, *(['--shelve'] if shelve else []), *commit_list])


def _new_per_commit(args: argparse.Namespace) -> None:
    """Create the changelist on the first commit since the base, then update it once per later commit."""
    workspace_dir = args.workspace_dir
    if args.no_edit:
        raise CommandError('--no-edit opens no files, so the steps of --per-commit can\'t add their commits to the '
                           'changelist. Leave out --no-edit.')
    if args.no_alias:
        raise CommandError('--per-commit names the changelist by its alias in the steps after the first. '
                           'Leave out --no-alias.')

    log.heading(f'Checking alias "{args.alias}"')
    check_alias_usable(args.alias, workspace_dir, args.force)
    log.success(f'Alias "{args.alias}" is usable')
    log.heading(f'Resolving base {args.base_given}')
    base_name = args.backend.base_name(args.base_given)
    log.success(base_name)

    aliases_before = dict(list_changelist_aliases(workspace_dir))

    def resumable() -> str | None:
        # Once the first step has saved the alias, its changelist exists.
        changelist = dict(list_changelist_aliases(
            workspace_dir)).get(args.alias)
        if changelist is None or changelist == aliases_before.get(args.alias):
            return None
        return f'CL {changelist} exists.'

    first, rest = _steps(args)
    run_per_commit(first, rest, args.base_branch, workspace_dir, args.backend,
                   sleep=SWARM_SHELF_DELAY if args.review or args.shelve else None, dry_run=args.dry_run,
                   message=args.message, message_file=args.file, resumable=resumable, base_name=base_name)
