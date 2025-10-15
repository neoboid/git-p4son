"""
Main CLI entry point for pergit.
"""

import argparse
import sys
import time
from . import __version__
from .sync import sync_command
from .edit import edit_command
from .changelist import changelist_command
from .list_changes import list_changes_command
from .review import review_command


def create_parser() -> argparse.ArgumentParser:
    """Create the main argument parser."""
    parser = argparse.ArgumentParser(
        prog='pergit',
        description='Utility for keeping a Perforce workspace and local git repo in sync',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  pergit sync 12345          # Sync with changelist 12345
  pergit sync latest         # Sync with the latest changelist affecting the workspace
  pergit sync last-synced    # Re-sync the last synced changelist
  pergit sync 12345 --force  # Force sync with writable files and allow older changelists
  pergit edit 12345          # Open git changes for edit in changelist 12345
  pergit edit 12345 --dry-run # Preview what would be opened for edit
  pergit changelist new -m "Fix bug" # Create new changelist with description
  pergit changelist new -m "Fix bug" -b main # Create changelist with commits since main
  pergit changelist update 12345 -b main # Update CL 12345 commit list
  pergit list-changes        # List commit subjects since HEAD~1
  pergit list-changes --base-branch main # List commit subjects since main branch
  pergit review new          # Create new changelist and Swarm review
  pergit review update 12345 # Update existing changelist and Swarm review
        """
    )

    parser.add_argument(
        '--version',
        action='version',
        version=f'pergit {__version__}'
    )

    parser.add_argument(
        '-s', '--sleep',
        help='Sleep for the specified number of seconds after the command is done.'
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
        help='Changelist to sync, "latest" to sync to the latest changelist affecting the workspace, or "last-synced" to re-sync the last synced changelist'
    )
    sync_parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Force sync encountered writable files and allow syncing to older changelists. '
             'When clobber is not enabled on your workspace, p4 will fail to sync files that '
             'are read-only. git removes the readonly flag on touched files. Also allows '
             'syncing to changelists older than the current one.'
    )

    # Edit subcommand
    edit_parser = subparsers.add_parser(
        'edit',
        help='Open local git changes for edit in Perforce',
        description='Find files that have changed and open for edit in p4. '
        'Finds common ancestor between base-branch and current branch, then opens '
        'files that changed on base branch but not on current branch.'
    )
    edit_parser.add_argument(
        'changelist',
        help='Changelist number to update'
    )
    edit_parser.add_argument(
        '-b', '--base-branch',
        default='HEAD~1',
        help='Base branch where p4 and git are in sync. Finds common ancestor with '
             'current branch and opens files that changed on base branch but not on '
             'current branch. Default is HEAD~1'
    )
    edit_parser.add_argument(
        '-n', '--dry-run',
        action='store_true',
        help='Pretend and print all commands, but do not execute'
    )

    # Changelist subcommand
    changelist_parser = subparsers.add_parser(
        'changelist',
        help='Manage Perforce changelists',
        description='Manage Perforce changelists.'
    )
    changelist_subparsers = changelist_parser.add_subparsers(
        dest='changelist_action',
        help='Available changelist actions',
        metavar='ACTION'
    )

    # changelist new
    changelist_new_parser = changelist_subparsers.add_parser(
        'new',
        help='Create a new Perforce changelist',
        description='Create a new Perforce changelist with a description and '
        'enumerated git commits since the base branch.'
    )
    changelist_new_parser.add_argument(
        '-m', '--message',
        required=True,
        help='Changelist description message'
    )
    changelist_new_parser.add_argument(
        '-b', '--base-branch',
        default='HEAD~1',
        help='Base branch for enumerating commits. Default is HEAD~1'
    )
    changelist_new_parser.add_argument(
        '-n', '--dry-run',
        action='store_true',
        help='Pretend and print what would be created, but do not execute'
    )

    # changelist update
    changelist_update_parser = changelist_subparsers.add_parser(
        'update',
        help='Update the commit list in an existing changelist',
        description='Update an existing Perforce changelist description by '
        'replacing the enumerated commit list with the current commits '
        'since the base branch. The user message is preserved.'
    )
    changelist_update_parser.add_argument(
        'changelist',
        help='Changelist number to update'
    )
    changelist_update_parser.add_argument(
        '-b', '--base-branch',
        default='HEAD~1',
        help='Base branch for enumerating commits. Default is HEAD~1'
    )
    changelist_update_parser.add_argument(
        '-n', '--dry-run',
        action='store_true',
        help='Pretend and print what would be updated, but do not execute'
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

    # Review subcommand
    review_parser = subparsers.add_parser(
        'review',
        help='Create or update Swarm reviews',
        description='Create new Swarm reviews or update existing ones with git changes'
    )
    review_subparsers = review_parser.add_subparsers(
        dest='review_action',
        help='Available review actions',
        metavar='ACTION'
    )

    # Review update subcommand
    review_update_parser = review_subparsers.add_parser(
        'update',
        help='Update existing changelist and Swarm review',
        description='Update an existing changelist with changes since base branch and update the Swarm review'
    )
    review_update_parser.add_argument(
        'changelist',
        help='Changelist number to update'
    )
    review_update_parser.add_argument(
        '-b', '--base-branch',
        default='HEAD~1',
        help='Base branch where p4 and git are in sync. Finds common ancestor with '
             'current branch and includes files that changed on base branch but not on '
             'current branch. Default is HEAD~1'
    )
    review_update_parser.add_argument(
        '-n', '--dry-run',
        action='store_true',
        help='Pretend and print all commands, but do not execute'
    )

    return parser


def run_command(args: argparse.Namespace) -> int:
    if args.command == 'sync':
        return sync_command(args)
    elif args.command == 'edit':
        return edit_command(args)
    elif args.command == 'changelist':
        return changelist_command(args)
    elif args.command == 'list-changes':
        return list_changes_command(args)
    elif args.command == 'review':
        return review_command(args)
    else:
        print(f'Unknown command: {args.command}', file=sys.stderr)
        return 1


def main() -> int:
    """Main entry point for the CLI."""
    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    try:
        exit_code = run_command(args)

        if exit_code == 0 and args.sleep is not None:
            seconds = int(args.sleep)
            print(f'Sleeping for {seconds} seconds')
            time.sleep(seconds)

        return exit_code
    except KeyboardInterrupt:
        print('\nOperation cancelled by user', file=sys.stderr)
        return 1
    except Exception as e:
        print(f'Error: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
