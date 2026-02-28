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


def alias_set_command(args: argparse.Namespace) -> int:
    """Execute the 'alias set' command."""
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


def alias_clean_command(args: argparse.Namespace) -> int:
    """Execute the 'alias clean' command with interactive prompts."""
    workspace_dir = args.workspace_dir

    aliases = list_changelist_aliases(workspace_dir)
    if not aliases:
        log.success('No changelist aliases to clean')
        return 0

    delete_all = False
    deleted_count = 0

    for name, changelist in aliases:
        # Interactive output stays as print() — it's a prompt-response UI
        print(f'{name} -> {changelist}')

        if delete_all:
            delete_changelist_alias(name, workspace_dir)
            deleted_count += 1
            log.info(f'  Deleted')
            continue

        quit = False
        while True:
            try:
                response = input(
                    'Delete? [y]es / [n]o / [a]ll / [q]uit: ').strip().lower()
            except EOFError:
                print()
                return 0

            if response in ('y', 'yes'):
                delete_changelist_alias(name, workspace_dir)
                deleted_count += 1
                log.info(f'  Deleted')
                break
            elif response in ('n', 'no'):
                break
            elif response in ('a', 'all'):
                delete_all = True
                delete_changelist_alias(name, workspace_dir)
                deleted_count += 1
                log.info(f'  Deleted')
                break
            elif response in ('q', 'quit'):
                quit = True
                break
            else:
                print('Please enter y, n, a, or q')
        if quit:
            break

    log.success(f'Deleted {deleted_count} alias(es)')
    return 0


def alias_command(args: argparse.Namespace) -> int:
    """Dispatch alias subcommands."""
    if args.alias_action == 'list':
        return alias_list_command(args)
    elif args.alias_action == 'set':
        return alias_set_command(args)
    elif args.alias_action == 'delete':
        return alias_delete_command(args)
    elif args.alias_action == 'clean':
        return alias_clean_command(args)
    else:
        log.error(
            'No alias action specified. Use "git-p4son alias -h" for help.')
        return 1
