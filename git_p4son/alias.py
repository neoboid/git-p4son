"""
Alias command implementation for git-p4son.
"""

import argparse
import sys
from .common import ensure_workspace
from .changelist_store import (
    save_changelist_alias,
    list_changelist_aliases,
    delete_changelist_alias,
)


def alias_list_command(args: argparse.Namespace) -> int:
    """
    Execute the 'alias list' command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = ensure_workspace()

    aliases = list_changelist_aliases(workspace_dir)
    if not aliases:
        print('No aliases defined')
        return 0

    for name, changelist in aliases:
        print(f'{name} -> {changelist}')

    return 0


def alias_set_command(args: argparse.Namespace) -> int:
    """
    Execute the 'alias set' command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = ensure_workspace()

    if not args.changelist.isdigit():
        print(f'Invalid changelist number: {args.changelist}', file=sys.stderr)
        return 1

    if not save_changelist_alias(args.alias, args.changelist,
                                 workspace_dir, force=args.force):
        return 1

    print(f'Saved alias "{args.alias}" -> {args.changelist}')
    return 0


def alias_delete_command(args: argparse.Namespace) -> int:
    """
    Execute the 'alias delete' command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = ensure_workspace()

    if not delete_changelist_alias(args.alias, workspace_dir):
        return 1

    print(f'Deleted alias "{args.alias}"')
    return 0


def alias_clean_command(args: argparse.Namespace) -> int:
    """
    Execute the 'alias clean' command with interactive prompts.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = ensure_workspace()

    aliases = list_changelist_aliases(workspace_dir)
    if not aliases:
        print('No aliases to clean')
        return 0

    delete_all = False
    deleted_count = 0

    for name, changelist in aliases:
        print(f'{name} -> {changelist}')

        if delete_all:
            delete_changelist_alias(name, workspace_dir)
            deleted_count += 1
            print(f'  Deleted')
            continue

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
                print(f'  Deleted')
                break
            elif response in ('n', 'no'):
                break
            elif response in ('a', 'all'):
                delete_all = True
                delete_changelist_alias(name, workspace_dir)
                deleted_count += 1
                print(f'  Deleted')
                break
            elif response in ('q', 'quit'):
                print(f'Deleted {deleted_count} alias(es)')
                return 0
            else:
                print('Please enter y, n, a, or q')

    print(f'Deleted {deleted_count} alias(es)')
    return 0


def alias_command(args: argparse.Namespace) -> int:
    """
    Dispatch alias subcommands.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    if args.alias_action == 'list':
        return alias_list_command(args)
    elif args.alias_action == 'set':
        return alias_set_command(args)
    elif args.alias_action == 'delete':
        return alias_delete_command(args)
    elif args.alias_action == 'clean':
        return alias_clean_command(args)
    else:
        print('No alias action specified. Use "git-p4son alias -h" for help.',
              file=sys.stderr)
        return 1
