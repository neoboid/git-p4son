"""Alias command implementation for git-p4son."""

import argparse
from .changelist_store import (
    load_changelist_alias,
    save_changelist_alias,
    list_changelist_aliases,
    delete_changelist_alias,
)
from .common import CommandError, prompt_choice
from .log import log


def _print_aliases(aliases: list[tuple[str, str]]) -> None:
    log.info(f'Found {len(aliases)}')
    for name, changelist in aliases:
        log.info(f'{name} -> {changelist}')


def alias_list_command(args: argparse.Namespace) -> int:
    """Execute the 'alias list' command."""
    aliases = list_changelist_aliases(args.workspace_dir)
    if not aliases:
        log.info('No aliases defined')
        return 0
    _print_aliases(aliases)
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

    save_changelist_alias(args.alias, args.changelist, workspace_dir,
                          force=args.force)
    log.success(f'{args.alias} -> {args.changelist}')
    return 0


def alias_delete_command(args: argparse.Namespace) -> int:
    """Execute the 'alias delete' command."""
    workspace_dir = args.workspace_dir

    if args.all:
        aliases = list_changelist_aliases(workspace_dir)
        if not aliases:
            log.success('No changelist aliases to delete')
            return 0
        _clean_all(aliases, workspace_dir)
        return 0

    delete_changelist_alias(args.alias, workspace_dir)
    log.success(f'Deleted alias "{args.alias}"')
    return 0


def _delete_listed(name: str, workspace_dir: str) -> bool:
    """Delete an alias found in the store; a failure is reported and the clean goes on."""
    try:
        delete_changelist_alias(name, workspace_dir)
    except CommandError as error:
        log.error(str(error))
        return False
    return True


def _clean_all(aliases: list[tuple[str, str]], workspace_dir: str) -> None:
    """Delete every alias without further prompting."""
    for name, _changelist in aliases:
        if _delete_listed(name, workspace_dir):
            log.success(f'Deleted "{name}"')


def _clean_interactive(aliases: list[tuple[str, str]],
                       workspace_dir: str) -> None:
    """Review each alias in turn with yes/no/all/quit prompts."""
    response = None
    for name, changelist in aliases:
        log.heading(f'{name} -> CL {changelist}')

        if response != 'all':
            response = prompt_choice(
                'Delete?', ['yes', 'no', 'all', 'quit'])

        if response is None or response == 'quit':
            log.info('Aborting')
            break
        elif response == 'no':
            log.info('Skipped')
            continue

        if _delete_listed(name, workspace_dir):
            log.success('Deleted')


def alias_clean_command(args: argparse.Namespace) -> int:
    """Execute the 'alias clean' command."""
    workspace_dir = args.workspace_dir

    aliases = list_changelist_aliases(workspace_dir)
    if not aliases:
        log.success('No changelist aliases to clean')
        return 0

    _print_aliases(aliases)

    mode = prompt_choice('Delete?', ['all', 'interactive', 'quit'])
    if mode is None or mode == 'quit':
        log.info('Aborting')
        return 0

    if mode == 'all':
        _clean_all(aliases, workspace_dir)
    else:
        _clean_interactive(aliases, workspace_dir)

    return 0
