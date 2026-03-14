"""
Alias command implementation for git-p4son.
"""

import argparse
from .changelist_store import (
    save_changelist_alias,
    list_changelist_aliases,
    delete_changelist_alias,
)
from .log import log


def alias_list_command(args: argparse.Namespace) -> int:
    """Execute the 'alias list' command."""
    workspace_dir = args.workspace_dir

    aliases = list_changelist_aliases(workspace_dir)
    if not aliases:
        log.info('No aliases defined')
        return 0

    for name, changelist in aliases:
        log.info(f'{name} -> {changelist}')

    log.success(f'{len(aliases)} listed')

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


def _prompt_delete(prompt: str) -> str | None:
    """Prompt until a valid y/n/a/q response is given. Returns None on EOF."""
    while True:
        try:
            response = input(prompt).strip().lower()
        except EOFError:
            print()
            return None

        if response in ('y', 'yes'):
            return 'yes'
        elif response in ('n', 'no'):
            return 'no'
        elif response in ('a', 'all'):
            return 'all'
        elif response in ('q', 'quit'):
            return 'quit'
        else:
            print('Please enter y, n, a, or q')


def alias_clean_command(args: argparse.Namespace) -> int:
    """Execute the 'alias clean' command with interactive prompts."""
    workspace_dir = args.workspace_dir

    aliases = list_changelist_aliases(workspace_dir)
    if not aliases:
        log.success('No changelist aliases to clean')
        return 0

    response = None
    for name, changelist in aliases:
        log.heading(f'{name} -> CL {changelist}')

        if response != 'all':
            response = _prompt_delete(
                'Delete? [y]es / [n]o / [a]ll / [q]uit: ')

        if response is None or response == 'quit':
            log.info('Aborting')
            break
        elif response == 'no':
            log.info('Skipped')
            continue

        if delete_changelist_alias(name, workspace_dir):
            log.success('Deleted')

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
