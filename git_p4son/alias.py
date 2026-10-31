"""Alias command implementation for git-p4son."""

import argparse
from .backend import Backend
from .changelist_store import (
    alias_changes,
    load_changelist_alias,
    save_changelist_alias,
    list_changelist_aliases,
    delete_changelist_alias,
    migrate_changelist_aliases,
)
from .common import CommandError
from .log import log, plural
from .perforce import is_changelist_gone


def _print_aliases(aliases: list[tuple[str, str]], workspace_dir: str, backend: Backend) -> None:
    """List the aliases, each with the jj change it records, marked gone when jj no longer has it."""
    changes = alias_changes(workspace_dir)
    existing = backend.existing_changes(list(changes.values()))
    log.info(f'Found {plural(len(aliases), "alias", "aliases")}')
    for name, changelist in aliases:
        line = f'{name} -> CL {changelist}'
        change = changes.get(name)
        if change is not None:
            line += f', jj change {change[:8]}' + \
                ('' if change in existing else ' (gone)')
        log.info(line)


def alias_list_command(args: argparse.Namespace) -> int:
    """Execute the 'alias list' command."""
    log.heading('Listing aliases')
    aliases = list_changelist_aliases(args.workspace_dir)
    if not aliases:
        log.info('No aliases defined')
        return 0
    _print_aliases(aliases, args.workspace_dir, args.backend)
    return 0


def alias_show_command(args: argparse.Namespace) -> int:
    """Execute the 'alias show' command."""
    print(load_changelist_alias(args.alias, args.workspace_dir))
    return 0


def alias_new_command(args: argparse.Namespace) -> int:
    """Execute the 'alias new' command."""
    workspace_dir = args.workspace_dir

    if not args.changelist.isdigit():
        raise CommandError(f'Invalid changelist number: {args.changelist}')

    log.heading(f'Saving alias {args.alias}')
    save_changelist_alias(args.alias, args.changelist, workspace_dir,
                          force=args.force)
    log.success(f'{args.alias} -> CL {args.changelist}')
    return 0


def alias_delete_command(args: argparse.Namespace) -> int:
    """Execute the 'alias delete' command."""
    workspace_dir = args.workspace_dir

    if args.all:
        log.heading('Deleting all aliases')
        aliases = list_changelist_aliases(workspace_dir)
        if not aliases:
            log.success('No changelist aliases to delete')
            return 0
        for name, _changelist in aliases:
            if _delete_listed(name, workspace_dir):
                log.success(f'Deleted alias "{name}"')
        return 0

    log.heading(f'Deleting alias {args.alias}')
    delete_changelist_alias(args.alias, workspace_dir)
    log.success(f'Deleted alias "{args.alias}"')
    return 0


def _delete_listed(name: str, workspace_dir: str) -> bool:
    """Delete an alias found in the store; a failure is reported and the caller goes on."""
    try:
        delete_changelist_alias(name, workspace_dir)
    except (CommandError, OSError) as error:
        log.error(str(error))
        return False
    return True


def alias_clean_command(args: argparse.Namespace) -> int:
    """Execute the 'alias clean' command."""
    workspace_dir = args.workspace_dir

    if not args.dry_run:
        moved = migrate_changelist_aliases(workspace_dir)
        if moved:
            log.heading('Moving old aliases')
            log.success(f'Moved {plural(moved, "alias", "aliases")} '
                        'to .git-p4son/aliases/')

    aliases = list_changelist_aliases(workspace_dir)
    if not aliases:
        log.success('No changelist aliases to clean')
        return 0

    gone: dict[str, bool] = {}
    for name, changelist in aliases:
        log.heading(f'{name} -> CL {changelist}')
        if changelist not in gone:
            gone[changelist] = is_changelist_gone(changelist, workspace_dir)
        if not gone[changelist] and args.dry_run:
            action = 'would-keep'
            log.success(f'Would keep, CL {changelist} is pending')
        elif not gone[changelist]:
            action = 'kept'
            log.success(f'Kept, CL {changelist} is pending')
        elif args.dry_run:
            action = 'would-delete'
            log.success(f'Would delete, CL {changelist} is not pending')
        elif _delete_listed(name, workspace_dir):
            action = 'deleted'
            log.success(f'Deleted, CL {changelist} is not pending')
        else:
            action = 'failed'
        if args.quiet:
            print(f'{action} {name} {changelist}')
    return 0
