"""
Main CLI entry point for pergit.
"""

import argparse
import sys
import time
from . import __version__
from .sync import sync_command
from .edit import edit_command
from .list_changes import list_changes_command


def create_parser():
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
  pergit edit new            # Create new changelist and open git changes for edit
  pergit edit 12345 --dry-run # Preview what would be opened for edit
  pergit list-changes        # List commit subjects since HEAD~1
  pergit list-changes --base-branch main # List commit subjects since main branch
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
        help='Changelist to update, or "new" to create a new changelist'
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

    return parser

def run_command(args):
    if args.command == 'sync':
        return sync_command(args)
    elif args.command == 'edit':
        return edit_command(args)
    elif args.command == 'list-changes':
        return list_changes_command(args)
    else:
        print(f'Unknown command: {args.command}', file=sys.stderr)
        return 1


def main():
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
