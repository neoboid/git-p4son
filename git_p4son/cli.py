"""
Main CLI entry point for git-p4son.
"""

import argparse
import os
import sys
import time
from importlib.resources import files
from . import __version__
from .sync import sync_command
from .sync_split import sync_split_command
from .sync_split_users import sync_split_users_command
from .new import new_command
from .update import update_command
from .list_changes import list_changes_command
from .alias import alias_command
from .init import init_command
from .review import review_command, sequence_editor_command
from .changelist_store import RESERVED_KEYWORDS
from .common import CommandError, RunError, branch_to_alias
from .git import get_current_branch, get_head_subject, get_workspace_dir
from .log import log
from .complete import run_complete
from .writable import writable_command


def create_parser() -> argparse.ArgumentParser:
    """Create the main argument parser."""
    parser = argparse.ArgumentParser(
        prog='git-p4son',
        description='Utility for keeping a Perforce workspace and local git repo in sync',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  git-p4son sync                # Sync to the latest changelist
  git-p4son sync head           # Sync to the latest changelist (explicit)
  git-p4son sync 12345          # Sync to changelist 12345
  git-p4son sync 123 156 178    # Sync each changelist in sequence, one commit each
  git-p4son sync 123 156 head   # Sync 123, 156, then the latest changelist
  git-p4son sync last-synced    # Re-sync the last synced changelist
  git-p4son sync --dry-run      # Show the changelists a sync would visit
  git-p4son sync -u alice       # Also split alice's changelists into commits of their own
  git-p4son sync --no-split     # Sync without splitting out the configured split users
  git-p4son sync-split          # Sync to latest, your own changelists split out
  git-p4son sync-split 12345    # Same, but stop at changelist 12345
  git-p4son sync-split -u alice -u bob  # Split out alice's and bob's changelists
  git-p4son sync-split-users    # List the users whose changelists sync splits out
  git-p4son sync-split-users add --me alice  # Split out your own and alice's changelists
  git-p4son new -m "Fix bug"    # Create changelist, alias defaults to branch name
  git-p4son new -m "Fix bug" --review  # Create changelist, create Swarm review
  git-p4son new -m "Fix bug" --no-alias # Create changelist without saving an alias
  git-p4son update --shelve     # Update changelist for current branch and re-shelve
  git-p4son update 12345        # Update changelist 12345
  git-p4son list-changes --base-branch main # List commit subjects since main branch
  git-p4son writable            # Show whether writable mode is on
  git-p4son writable enable     # Keep git-tracked files writable
  git-p4son writable apply      # Make tracked files match the writable mode
        """
    )

    parser.add_argument(
        '--version',
        action='version',
        version=f'git-p4son {__version__}'
    )

    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        default=False,
        help='Show verbose output (commands, elapsed times, raw subprocess output)'
    )

    subparsers = parser.add_subparsers(
        dest='command',
        help='Available commands',
        metavar='COMMAND'
    )

    # Init subcommand
    subparsers.add_parser(
        'init',
        help='Initialize a git repository inside a Perforce workspace',
        description='Set up a new git repository inside a Perforce workspace. '
        'Checks preconditions (P4 workspace), initializes git, '
        'sets up .gitignore, and creates an initial commit.'
    )

    # Sync subcommand
    sync_parser = subparsers.add_parser(
        'sync',
        help='Sync local git repository with a Perforce workspace',
        description='Sync local git repository with a Perforce workspace'
    )
    sync_parser.add_argument(
        'changelist',
        nargs='*',
        default=[],
        metavar='CHANGELIST',
        help='Changelist number(s) to sync, "last-synced" to re-sync the last synced changelist, '
             'or "head" to sync to the latest changelist. Give several strictly increasing '
             'changelist numbers to sync each in sequence, one commit per changelist; "head" may '
             'close out such a sequence as a trailing target. Omit to sync to the latest '
             'changelist affecting the workspace'
    )
    sync_parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Allow syncing to changelists older than the current one.'
    )
    sync_parser.add_argument(
        '-n', '--dry-run',
        action='store_true',
        help='Print the resolved sync sequence without syncing'
    )
    sync_parser.add_argument(
        '-u', '--split-user',
        action='append',
        default=None,
        metavar='NAME',
        help='Also split out this Perforce user\'s changelists into commits '
             'of their own, on top of the configured split users. Repeat to '
             'give several users'
    )
    sync_parser.add_argument(
        '--no-split',
        action='store_true',
        help='Ignore the configured split users for this sync. Users given '
             'with --split-user are still split out'
    )

    # Sync-split subcommand
    sync_split_parser = subparsers.add_parser(
        'sync-split',
        help='Sync forward, splitting a user\'s changelists into own commits',
        description='Sync from the last synced changelist up to a target '
        'changelist (the latest by default), syncing the changelist submitted '
        'just before each of the selected users\' submits first so that every '
        'changelist they submitted lands in a git commit of its own.'
    )
    sync_split_parser.add_argument(
        'changelist',
        nargs='?',
        default=None,
        metavar='CHANGELIST',
        help='Changelist number to sync up to, or "head" for the latest. '
             'Omit to sync to the latest changelist affecting the workspace'
    )
    sync_split_parser.add_argument(
        '-u', '--user',
        action='append',
        default=None,
        metavar='NAME',
        help='Perforce user whose changelists to split into their own '
             'commits. Repeat to select several users. '
             'Defaults to the current p4 user'
    )
    sync_split_parser.add_argument(
        '-n', '--dry-run',
        action='store_true',
        help='Print the resolved sync sequence without syncing'
    )

    # Sync-split-users subcommand
    split_users_parser = subparsers.add_parser(
        'sync-split-users',
        help='Show or edit the users whose changelists sync splits out',
        description='Split users are the Perforce users whose submitted '
        'changelists sync gives a git commit each, holding nothing but that '
        'change. Without an action, lists them.'
    )
    split_users_subparsers = split_users_parser.add_subparsers(
        dest='split_users_action',
        help='Available split user actions',
        metavar='ACTION'
    )
    split_users_subparsers.add_parser(
        'list',
        help='List the split users',
        description='List the split users. $(user) stands for the current '
        'Perforce user and is shown with the name it resolves to.'
    )
    split_users_add_parser = split_users_subparsers.add_parser(
        'add',
        help='Add split users',
        description='Add users to the split users. Each name must be a '
        'Perforce user; if any is not, nothing is added.'
    )
    split_users_add_parser.add_argument(
        'names',
        nargs='*',
        metavar='NAME',
        help='Perforce user name. A quoted "$(user)" adds the current user, '
             'like --me'
    )
    split_users_add_parser.add_argument(
        '--me',
        action='store_true',
        help='Add the current Perforce user, stored as $(user) so it follows '
             'whoever is logged in'
    )
    split_users_delete_parser = split_users_subparsers.add_parser(
        'delete',
        help='Remove split users',
        description='Remove users from the split users. If any name is not '
        'a split user, nothing is removed.'
    )
    split_users_delete_parser.add_argument(
        'names',
        nargs='*',
        metavar='NAME',
        help='Split user to remove. A quoted "$(user)" removes the current '
             'user entry, like --me'
    )
    split_users_delete_parser.add_argument(
        '--me',
        action='store_true',
        help='Remove the $(user) entry for the current Perforce user'
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
        default=None,
        help='Changelist description message (defaults to HEAD commit subject)'
    )
    new_parser.add_argument(
        '-b', '--base-branch',
        default='HEAD~1',
        help='Base branch for enumerating commits and finding changed files. Default is HEAD~1'
    )
    new_parser.add_argument(
        'alias',
        nargs='?',
        default='branch',
        help='Alias name to save the new changelist number under. '
             'Defaults to the current branch name'
    )
    new_parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Overwrite an existing alias file'
    )
    new_parser.add_argument(
        '--no-alias',
        action='store_true',
        help='Skip saving a changelist alias'
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
        type=int,
        help='Sleep for the specified number of seconds after the command is done'
    )

    # Update subcommand
    update_parser = subparsers.add_parser(
        'update',
        help='Update an existing changelist description and open files for edit',
        description='Update an existing Perforce changelist description: '
        'commits since the base branch replace their existing entries in '
        'the enumerated commit list and new ones are appended; entries '
        'outside the range are kept. By default also opens changed files '
        'for edit.'
    )
    update_parser.add_argument(
        'changelist',
        nargs='?',
        default='branch',
        help='Changelist number or named alias to update. '
             'Defaults to the current branch name'
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
        '--no-desc',
        action='store_true',
        help='Skip updating the changelist description'
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
        type=int,
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

    # alias new
    alias_new_parser = alias_subparsers.add_parser(
        'new',
        help='Save a changelist number under a named alias',
        description='Save a changelist number under a named alias in '
        '.git-p4son/changelists/<alias>'
    )
    alias_new_parser.add_argument(
        'changelist',
        help='Changelist number to save'
    )
    alias_new_parser.add_argument(
        'alias',
        nargs='?',
        default='branch',
        help='Alias name to save the changelist number under. '
             'Defaults to the current branch name'
    )
    alias_new_parser.add_argument(
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
        nargs='?',
        default='branch',
        help='Alias name to delete. Defaults to the current branch name'
    )

    # alias clean
    alias_subparsers.add_parser(
        'clean',
        help='Clean up changelist aliases',
        description='List all changelist aliases, then delete all of them '
        'or review each one interactively with yes/no/all/quit prompts'
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
        nargs='?',
        default='branch',
        help='Alias name for the new changelist. Defaults to the current branch name'
    )
    review_parser.add_argument(
        '-m', '--message',
        default=None,
        help='Changelist description message (defaults to HEAD commit subject)'
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

    # Writable subcommand
    writable_parser = subparsers.add_parser(
        'writable',
        help='Show or apply writable mode for git-tracked files',
        description='Writable mode keeps git-tracked files writable so they '
        'can be edited without a manual p4 edit. Git-ignored files are left '
        'to Perforce. Without an action, shows whether the mode is on.'
    )
    writable_subparsers = writable_parser.add_subparsers(
        dest='writable_action',
        help='Available writable actions',
        metavar='ACTION'
    )
    writable_subparsers.add_parser(
        'enable',
        help='Turn writable mode on and make git-tracked files writable',
        description='Turn writable mode on, then make every git-tracked file '
        'writable. Sync keeps the files it syncs writable from then on.'
    )
    writable_subparsers.add_parser(
        'disable',
        help='Turn writable mode off and make git-tracked files read-only',
        description='Turn writable mode off, then make git-tracked files '
        'read-only, except files opened in Perforce.'
    )
    writable_subparsers.add_parser(
        'apply',
        help='Make git-tracked files match the writable mode',
        description='With writable mode on, make every git-tracked file '
        'writable. With it off, make them read-only, except files opened in '
        'Perforce. Use it to fix files made read-only outside git-p4son, '
        'for example by a submit.'
    )

    # Completion subcommand (prints shell completion script path)
    completion_parser = subparsers.add_parser(
        'completion',
        help='Print path to a shell completion script',
        description='Print the path to a shell completion script '
        'for bash, zsh, or powershell.'
    )
    completion_parser.add_argument(
        'shell',
        choices=['bash', 'zsh', 'powershell'],
        help='Shell to print completion script path for'
    )
    completion_parser.add_argument(
        '-d', '--dirname',
        action='store_true',
        help='Print the directory instead of the full file path'
    )

    # Hidden _sequence-editor subcommand (used internally by review). It is
    # given no help: argparse lists a subcommand with help=SUPPRESS anyway,
    # as "==SUPPRESS==", but leaves one without help out of the list.
    seq_editor_parser = subparsers.add_parser(
        '_sequence-editor',
    )
    seq_editor_parser.add_argument(
        'filename',
        help='The rebase todo file to edit'
    )

    return parser


def _resolve_branch_keyword(workspace_dir: str) -> str | None:
    """Resolve current git branch to an alias name.

    Returns the resolved alias name, or None if resolution fails.
    Prints an error message on failure.
    """
    branch = get_current_branch(workspace_dir)
    if not branch:
        log.error(
            'Cannot resolve branch name on detached HEAD. '
            'Use --no-alias or supply an explicit alias name.')
        return None
    alias = branch_to_alias(branch)
    if alias in RESERVED_KEYWORDS:
        log.error(f'branch "{branch}" resolves to reserved keyword "{alias}"')
        return None
    return alias


_COMPLETION_FILES = {
    'bash': 'git-p4son.bash',
    'zsh': '_git-p4son',
    'powershell': 'git-p4son.ps1',
}


def completion_command(args: argparse.Namespace) -> int:
    """Print path to a shell completion script."""
    filename = _COMPLETION_FILES[args.shell]
    completions_dir = files('git_p4son') / 'completions'

    if args.dirname:
        print(completions_dir)
    else:
        print(completions_dir / filename)

    return 0


def run_command(args: argparse.Namespace) -> int:
    args.invocation_dir = os.getcwd()

    log.heading('Finding workspace directory')
    args.workspace_dir = get_workspace_dir()
    if not args.workspace_dir:
        log.error('No .git directory found in current or parent directories')
        return 1
    log.success(args.workspace_dir)

    # Handle --no-alias for new/review
    if args.command in ('new', 'review') and getattr(args, 'no_alias', False):
        args.alias = None

    # Determine which attribute may need branch resolution
    branch_attr = None
    if args.command in ('new', 'review') and getattr(args, 'alias', None) == 'branch':
        branch_attr = 'alias'
    elif args.command == 'update' and args.changelist == 'branch':
        branch_attr = 'changelist'
    elif (args.command == 'alias'
          and args.alias_action in ('new', 'delete')
          and args.alias == 'branch'):
        branch_attr = 'alias'

    if branch_attr:
        log.heading('Resolving alias from current git branch')
        resolved = _resolve_branch_keyword(args.workspace_dir)
        if resolved is None:
            return 1
        setattr(args, branch_attr, resolved)
        log.success(f'branch -> {resolved}')

    if args.command in ('new', 'review') and args.message is None:
        log.heading('Resolving message from HEAD commit')
        args.message = get_head_subject(args.workspace_dir)
        if args.message is None:
            log.error('No commits found to derive message from')
            return 1
        log.success(args.message)

    if args.command == 'sync':
        return sync_command(args)
    elif args.command == 'sync-split':
        return sync_split_command(args)
    elif args.command == 'sync-split-users':
        return sync_split_users_command(args)
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
    elif args.command == 'writable':
        return writable_command(args)
    elif args.command == '_sequence-editor':
        return sequence_editor_command(args)
    else:
        log.error(f'Unknown command: {args.command}')
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

    if args.command == 'completion':
        return completion_command(args)

    try:
        log.verbose_mode = args.verbose

        # Run init before run_command (as no workspace needed)
        if args.command == 'init':
            return init_command(args)

        exit_code = run_command(args)

        if exit_code == 0 and getattr(args, 'sleep', None) is not None:
            seconds = args.sleep
            log.heading(f'Sleeping for {seconds} seconds')
            time.sleep(seconds)
            log.success('awake again')

        return exit_code
    except KeyboardInterrupt:
        log.error('\nOperation cancelled by user')
        return 1
    except RunError as e:
        if e.stderr:
            for line in e.stderr:
                log.error(line)
        log.error(f'Process failed with return code {e.returncode}')
        return e.returncode
    except CommandError as e:
        log.error(str(e))
        return e.returncode
    except Exception as e:
        log.error(str(e))
        return 1


if __name__ == '__main__':
    sys.exit(main())
