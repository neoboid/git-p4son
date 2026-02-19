"""
Main CLI entry point for git-p4son.
"""

import argparse
import sys
import time
from . import __version__
from .sync import sync_command
from .new import new_command
from .update import update_command
from .list_changes import list_changes_command
from .alias import alias_command
from .review import review_command, sequence_editor_command
from .changelist_store import RESERVED_KEYWORDS
from .common import CommandError, RunError, branch_to_alias, get_current_branch, get_workspace_dir
from .complete import run_complete


def create_parser() -> argparse.ArgumentParser:
    """Create the main argument parser."""
    parser = argparse.ArgumentParser(
        prog='git-p4son',
        description='Utility for keeping a Perforce workspace and local git repo in sync',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  git-p4son sync 12345          # Sync with changelist 12345
  git-p4son sync latest         # Sync with the latest changelist affecting the workspace
  git-p4son sync last-synced    # Re-sync the last synced changelist
  git-p4son sync 12345 --force  # Force sync with writable files and allow older changelists
  git-p4son new -m "Fix bug"    # Create changelist, open files for edit
  git-p4son new -m "Fix bug" --review  # Create changelist, open files, create Swarm review
  git-p4son new -m "Fix bug" --no-edit # Create changelist only, don't open files
  git-p4son update 12345        # Update changelist description and open files for edit
  git-p4son update myalias --shelve    # Update changelist and re-shelve
  git-p4son list-changes        # List commit subjects since HEAD~1
  git-p4son list-changes --base-branch main # List commit subjects since main branch
        """
    )

    parser.add_argument(
        '--version',
        action='version',
        version=f'git-p4son {__version__}'
    )

    subparsers = parser.add_subparsers(
        dest='command',
        help='Available commands',
        metavar='COMMAND'
    )

    # Sync subcommand
    sync_parser = subparsers.add_parser(
        'sync',
        help='Sync local git repository with a Perforce workspace',
        description='Sync local git repository with a Perforce workspace'
    )
    sync_parser.add_argument(
        'changelist',
        help='Changelist number or named alias to sync, "latest" to sync to the latest changelist affecting the workspace, or "last-synced" to re-sync the last synced changelist'
    )
    sync_parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Force sync encountered writable files and allow syncing to older changelists. '
             'When clobber is not enabled on your workspace, p4 will fail to sync files that '
             'are read-only. git removes the readonly flag on touched files. Also allows '
             'syncing to changelists older than the current one.'
    )

    # New subcommand
    new_parser = subparsers.add_parser(
        'new',
        help='Create a new changelist, open files for edit, and optionally create a Swarm review',
        description='Create a new Perforce changelist with a description and '
        'enumerated git commits since the base branch. By default also opens '
        'changed files for edit in the changelist.'
    )
    new_parser.add_argument(
        '-m', '--message',
        required=True,
        help='Changelist description message'
    )
    new_parser.add_argument(
        '-b', '--base-branch',
        default='HEAD~1',
        help='Base branch for enumerating commits and finding changed files. Default is HEAD~1'
    )
    new_parser.add_argument(
        'alias',
        nargs='?',
        default=None,
        help='Optional alias name to save the new changelist number under'
    )
    new_parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Overwrite an existing alias file'
    )
    new_parser.add_argument(
        '-n', '--dry-run',
        action='store_true',
        help='Pretend and print what would be done, but do not execute'
    )
    new_parser.add_argument(
        '--no-edit',
        action='store_true',
        help='Skip opening changed files for edit in Perforce'
    )
    new_parser.add_argument(
        '--shelve',
        action='store_true',
        help='Shelve the changelist after creating it'
    )
    new_parser.add_argument(
        '--review',
        action='store_true',
        help='Add #review keyword and shelve to create a Swarm review'
    )
    new_parser.add_argument(
        '-s', '--sleep',
        help='Sleep for the specified number of seconds after the command is done'
    )

    # Update subcommand
    update_parser = subparsers.add_parser(
        'update',
        help='Update an existing changelist description and open files for edit',
        description='Update an existing Perforce changelist description by '
        'replacing the enumerated commit list with the current commits '
        'since the base branch. By default also opens changed files for edit.'
    )
    update_parser.add_argument(
        'changelist',
        help='Changelist number or named alias to update'
    )
    update_parser.add_argument(
        '-b', '--base-branch',
        default='HEAD~1',
        help='Base branch for enumerating commits and finding changed files. Default is HEAD~1'
    )
    update_parser.add_argument(
        '-n', '--dry-run',
        action='store_true',
        help='Pretend and print what would be done, but do not execute'
    )
    update_parser.add_argument(
        '--no-edit',
        action='store_true',
        help='Skip opening changed files for edit in Perforce'
    )
    update_parser.add_argument(
        '--shelve',
        action='store_true',
        help='Re-shelve the changelist after updating'
    )
    update_parser.add_argument(
        '-s', '--sleep',
        help='Sleep for the specified number of seconds after the command is done'
    )

    # List-changes subcommand
    list_changes_parser = subparsers.add_parser(
        'list-changes',
        help='List commit subjects since base branch',
        description='List commit subjects since base branch in chronological order (oldest first)'
    )
    list_changes_parser.add_argument(
        '-b', '--base-branch',
        default='HEAD~1',
        help='Base branch to compare against. Default is HEAD~1'
    )

    # Alias subcommand
    alias_parser = subparsers.add_parser(
        'alias',
        help='Manage changelist aliases',
        description='Manage changelist aliases stored in .git-p4son/changelists/'
    )
    alias_subparsers = alias_parser.add_subparsers(
        dest='alias_action',
        help='Available alias actions',
        metavar='ACTION'
    )

    # alias list
    alias_subparsers.add_parser(
        'list',
        help='List all aliases and their changelist numbers',
        description='List all changelist aliases stored in .git-p4son/changelists/'
    )

    # alias set
    alias_set_parser = alias_subparsers.add_parser(
        'set',
        help='Save a changelist number under a named alias',
        description='Save a changelist number under a named alias in '
        '.git-p4son/changelists/<alias>'
    )
    alias_set_parser.add_argument(
        'changelist',
        help='Changelist number to save'
    )
    alias_set_parser.add_argument(
        'alias',
        help='Alias name to save the changelist number under'
    )
    alias_set_parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Overwrite an existing alias file'
    )

    # alias delete
    alias_delete_parser = alias_subparsers.add_parser(
        'delete',
        help='Delete a changelist alias',
        description='Delete a changelist alias from .git-p4son/changelists/'
    )
    alias_delete_parser.add_argument(
        'alias',
        help='Alias name to delete'
    )

    # alias clean
    alias_subparsers.add_parser(
        'clean',
        help='Interactive cleanup of changelist aliases',
        description='Interactively review and delete changelist aliases with '
        'yes/no/all/quit prompts'
    )

    # Review subcommand
    review_parser = subparsers.add_parser(
        'review',
        help='Create a Swarm review via automated interactive rebase',
        description='Automate the interactive rebase workflow by generating '
        'a rebase todo with exec lines that run git p4son new/update '
        'for each commit since the base branch.'
    )
    review_parser.add_argument(
        'alias',
        help='Alias name for the new changelist'
    )
    review_parser.add_argument(
        '-m', '--message',
        required=True,
        help='Changelist description message'
    )
    review_parser.add_argument(
        '-b', '--base-branch',
        default='HEAD~1',
        help='Base branch to rebase onto and find commits since. Default is HEAD~1'
    )
    review_parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Overwrite an existing alias file'
    )
    review_parser.add_argument(
        '-n', '--dry-run',
        action='store_true',
        help='Print the generated rebase todo without executing'
    )

    # Hidden _sequence-editor subcommand (used internally by review)
    seq_editor_parser = subparsers.add_parser(
        '_sequence-editor',
        help=argparse.SUPPRESS,
    )
    seq_editor_parser.add_argument(
        'filename',
        help='The rebase todo file to edit'
    )

    return parser


def _resolve_branch_keyword(value: str, workspace_dir: str) -> str | None:
    """Resolve 'branch' keyword to the branch-derived alias name.

    Returns the resolved alias name, or None if resolution fails.
    Prints an error message on failure.
    """
    if value != 'branch':
        return value
    branch = get_current_branch(workspace_dir)
    if not branch or branch == 'main':
        print('Error: "branch" keyword cannot be used on main or detached HEAD',
              file=sys.stderr)
        return None
    alias = branch_to_alias(branch)
    if alias in RESERVED_KEYWORDS:
        print(f'Error: branch "{branch}" resolves to reserved keyword "{alias}"',
              file=sys.stderr)
        return None
    return alias


def _resolve_branch_alias(args: argparse.Namespace) -> int | None:
    """Resolve 'branch' keyword in args.alias. Returns error code or None on success."""
    if getattr(args, 'alias', None) != 'branch':
        return None
    resolved = _resolve_branch_keyword('branch', args.workspace_dir)
    if resolved is None:
        return 1
    args.alias = resolved
    return None


def run_command(args: argparse.Namespace) -> int:
    args.workspace_dir = get_workspace_dir()
    if not args.workspace_dir:
        print('Failed to find workspace root directory', file=sys.stderr)
        return 1

    if args.command in ('new', 'review'):
        error = _resolve_branch_alias(args)
        if error is not None:
            return error

    if args.command == 'update':
        resolved = _resolve_branch_keyword(args.changelist, args.workspace_dir)
        if resolved is None:
            return 1
        args.changelist = resolved

    if args.command == 'alias' and args.alias_action == 'set':
        resolved = _resolve_branch_keyword(args.alias, args.workspace_dir)
        if resolved is None:
            return 1
        args.alias = resolved

    if args.command == 'sync':
        return sync_command(args)
    elif args.command == 'new':
        return new_command(args)
    elif args.command == 'update':
        return update_command(args)
    elif args.command == 'list-changes':
        return list_changes_command(args)
    elif args.command == 'alias':
        return alias_command(args)
    elif args.command == 'review':
        return review_command(args)
    elif args.command == '_sequence-editor':
        return sequence_editor_command(args)
    else:
        print(f'Unknown command: {args.command}', file=sys.stderr)
        return 1


def main() -> int:
    """Main entry point for the CLI."""
    # Handle 'complete' before argparse to avoid flag/word conflicts
    if len(sys.argv) >= 2 and sys.argv[1] == 'complete':
        words = sys.argv[2:]
        # Strip leading '--' separator if present
        if words and words[0] == '--':
            words = words[1:]
        return run_complete(words)

    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    try:
        exit_code = run_command(args)

        if exit_code == 0 and getattr(args, 'sleep', None) is not None:
            seconds = int(args.sleep)
            print(f'Sleeping for {seconds} seconds')
            time.sleep(seconds)

        return exit_code
    except KeyboardInterrupt:
        print('\nOperation cancelled by user', file=sys.stderr)
        return 1
    except RunError as e:
        for line in e.stderr:
            print(line, file=sys.stderr)
        print(f'Command failed with exit code {e.returncode}', file=sys.stderr)
        return e.returncode
    except CommandError as e:
        print(str(e), file=sys.stderr)
        return e.returncode
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
