"""
Alias command implementation for git-p4son.
"""

import argparse
from .changelist_store import (
    save_changelist_alias,
    list_changelist_aliases,
    delete_changelist_alias,
)
from .common import prompt_choice
from .log import log


def alias_list_command(args: argparse.Namespace) -> int:
    """Execute the 'alias list' command."""
    workspace_dir = args.workspace_dir

    aliases = list_changelist_aliases(workspace_dir)
    if not aliases:
        log.info('No aliases defined')
        return 0
    log.info(f'Found {len(aliases)}')

    for name, changelist in aliases:
        log.info(f'{name} -> {changelist}')

    return 0


def alias_new_command(args: argparse.Namespace) -> int:
    """Execute the 'alias new' command."""
    workspace_dir = args.workspace_dir

    if not args.changelist.isdigit():
        log.error(f'Invalid changelist number: {args.changelist}')
        return 1

    if not save_changelist_alias(args.alias, args.changelist,
                                 workspace_dir, force=args.force):
        return 1

    log.success(f'{args.alias} -> {args.changelist}')
    return 0


def alias_delete_command(args: argparse.Namespace) -> int:
    """Execute the 'alias delete' command."""
    workspace_dir = args.workspace_dir

    if not delete_changelist_alias(args.alias, workspace_dir):
        return 1

    log.success(f'Deleted alias "{args.alias}"')
    return 0


def _clean_all(aliases: list[tuple[str, str]], workspace_dir: str) -> None:
    """Delete every alias without further prompting."""
    for name, _changelist in aliases:
        if delete_changelist_alias(name, workspace_dir):
            log.success(f'Deleted "{name}"')


def _clean_interactive(aliases: list[tuple[str, str]],
                       workspace_dir: str) -> None:
    """Review each alias in turn with yes/no/all/quit prompts.

    The all option lets the user keep the first aliases and sweep the rest:
    answer no until the last one worth keeping, then a deletes the
    remainder without further prompts."""
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

        if delete_changelist_alias(name, workspace_dir):
            log.success('Deleted')


def alias_clean_command(args: argparse.Namespace) -> int:
    """Execute the 'alias clean' command."""
    workspace_dir = args.workspace_dir

    aliases = list_changelist_aliases(workspace_dir)
    if not aliases:
        log.success('No changelist aliases to clean')
        return 0

    log.info(f'Found {len(aliases)}')
    for name, changelist in aliases:
        log.info(f'{name} -> {changelist}')

    mode = prompt_choice('Delete?', ['all', 'interactive', 'quit'])
    if mode is None or mode == 'quit':
        log.info('Aborting')
        return 0

    if mode == 'all':
        _clean_all(aliases, workspace_dir)
    else:
        _clean_interactive(aliases, workspace_dir)

    return 0


def alias_command(args: argparse.Namespace) -> int:
    """Dispatch alias subcommands."""
    if args.alias_action == 'list':
        return alias_list_command(args)
    elif args.alias_action == 'new':
        return alias_new_command(args)
    elif args.alias_action == 'delete':
        return alias_delete_command(args)
    elif args.alias_action == 'clean':
        return alias_clean_command(args)
    else:
        log.error(
            'No alias action specified. Use "git-p4son alias -h" for help.')
        return 1
