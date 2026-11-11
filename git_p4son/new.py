"""New command: creates a Perforce changelist from the git change, at once or commit by commit."""

import argparse
import shlex
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
from .lib import check_at_latest_revision, create_changelist, edit_and_shelve
from .log import log
from .per_commit import run_per_commit
from .review_tools import push_review, resolve_review_tool, shelf_delay


def new_command(args: argparse.Namespace) -> int:
    """Execute the new command."""
    workspace_dir = args.workspace_dir
    args.review = args.review or args.review_tool is not None
    tool = _review_tool(args)
    if args.shelve or args.review:
        check_at_latest_revision(
            args, args.base_branch, None, args.backend.revision)
    if args.per_commit:
        _new_per_commit(args, tool)
        return 0
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
        commit_list=not args.no_commit_list, review=args.review and tool is None,
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
                                  record=replace(record, base_commit='', included=[]), review_tool=tool)
        log.success(f'Created CL {changelist}{alias_note}')

    edit_and_shelve(changelist, args.base_branch, workspace_dir,
                    edit=not args.no_edit, shelve=args.shelve or args.review,
                    dry_run=args.dry_run, revision=revision)
    if tool is not None:
        try:
            push_review(args.alias, tool, changelist, first=True,
                        workspace_dir=workspace_dir, dry_run=args.dry_run)
        except CommandError as error:
            # A per-commit run gives its own resume hint, which shelves the commits one by one.
            if args.per_commit_step:
                raise
            rerun = shlex.join(
                ['git', 'p4son', 'update', args.alias, '--shelve', *_ignore_out_of_date(args)])
            raise CommandError(f'{error}\nCL {changelist} is created and shelved. Once the tool works, run: {rerun}',
                               error.returncode) from error
    if record is not None:
        update_record(args.alias, record, workspace_dir)
    return 0


def _review_tool(args: argparse.Namespace) -> str | None:
    """The tool new runs after each shelve, None for Swarm or without --review; raise CommandError if refused."""
    if not args.review:
        return None
    tool = resolve_review_tool(args.review_tool, args.workspace_dir)
    if tool is not None and args.no_alias:
        raise CommandError(f'Review tool {tool} needs the alias to record the tool for later updates. '
                           'Leave out --no-alias.')
    return tool


def _check_one_alias_per_change(alias: str, change: str | None, workspace_dir: str, force: bool) -> None:
    """Raise CommandError if another alias records change, unless force moves the record to alias."""
    name = None if change is None else alias_for_change(change, workspace_dir)
    if name is None or name == alias or force:
        return
    changelist = load_changelist_alias(name, workspace_dir)
    raise CommandError(f'Alias "{name}" already records this jj change, for CL {changelist}. Run '
                       f'"git p4son update {name}" to update it, or pass --force to move the record to "{alias}".')


def _ignore_out_of_date(args: argparse.Namespace) -> list[str]:
    """--ignore-out-of-date if given, for the commands that carry on this run."""
    return ['--ignore-out-of-date'] if args.ignore_out_of_date else []


def _steps(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """The new step for the first commit and the update step for the others, both shelving if new would."""
    shelve = (['--review-tool', args.review_tool] if args.review_tool is not None
              else ['--review'] if args.review else ['--shelve'] if args.shelve else [])
    commit_list = ['--no-commit-list'] if args.no_commit_list else []
    force = ['--force'] if args.force else []
    ignore = _ignore_out_of_date(args)
    return (['new', args.alias, *shelve, *force, *commit_list, *ignore],
            ['update', args.alias, *(['--shelve'] if shelve else []), *commit_list, *ignore])


def _new_per_commit(args: argparse.Namespace, tool: str | None) -> None:
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
    log.heading(f'Resolving base name from {args.base_given}')
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
                   sleep=shelf_delay(tool) if args.review or args.shelve else None, dry_run=args.dry_run,
                   message=args.message, message_file=args.file, resumable=resumable, base_name=base_name,
                   base_to='the first step')
