"""Main CLI entry point for git-p4son."""

import argparse
import os
import sys
from importlib.resources import files
from typing import cast
from . import __version__
from .backend import BACKENDS, open_backend
from .sync import sync_command
from .sync_split_users import sync_split_users_command
from .new import new_command
from .update import update_command
from .list_changes import list_changes_command
from .alias import (
    alias_clean_command,
    alias_delete_command,
    alias_list_command,
    alias_new_command,
    alias_show_command,
)
from .init import init_command
from .review import review_command
from .common import CommandError, RunError
from .git import get_workspace_dir
from .log import log
from .complete import run_complete
from .skill import skill_install_command, skill_show_command
from .writable import writable_command


def _add_base_branch(parser: argparse.ArgumentParser, help: str) -> None:
    """Add -b/--base-branch, defaulting to None, which run_command resolves to the backend's default."""
    parser.add_argument('-b', '--base-branch',
                        help=f'{help}. Defaults to HEAD~1, '
                        'or with the jj backend the parent of the jj change')


def _add_branch_keyword_arg(parser: argparse.ArgumentParser, dest: str, help: str,
                            group: argparse._MutuallyExclusiveGroup | None = None) -> None:
    """Add an optional positional that defaults to the branch keyword, which run_command resolves."""
    (group or parser).add_argument(
        dest, nargs='?', default='branch',
        help=f'{help}. Defaults to the current branch name, or with the jj backend its bookmark')
    parser.set_defaults(branch_keyword_arg=dest)


def _add_dry_run(parser: argparse.ArgumentParser, help: str) -> None:
    """Add -n/--dry-run."""
    parser.add_argument('-n', '--dry-run', action='store_true', help=help)


def _add_no_edit(parser: argparse.ArgumentParser) -> None:
    """Add --no-edit, which skips opening files for edit."""
    parser.add_argument('--no-edit', action='store_true',
                        help='Skip opening changed files for edit in Perforce')


def _add_message_args(parser: argparse.ArgumentParser, message_help: str,
                      file_help: str, from_head: bool = False) -> None:
    """Add -m/--message and -F/--file; with from_head, run_command defaults to the HEAD subject."""
    message = parser.add_mutually_exclusive_group()
    message.add_argument('-m', '--message', help=message_help)
    message.add_argument('-F', '--file', help=file_help)
    parser.set_defaults(message_from_head=from_head)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='git-p4son',
        description='Keep a local git repository in sync with a Perforce workspace.',
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
  git-p4son skill install       # Install the Claude Code skill for git-p4son
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
        help='Show verbose output (commands, elapsed times, raw subprocess output)'
    )
    parser.add_argument(
        '--backend', choices=BACKENDS, dest='backend_choice',
        help='Follow git or jj for the change, the base and the branch, overriding core.backend; '
        'git in a jj workspace still holds jj\'s working-copy lock'
    )

    subparsers = parser.add_subparsers(
        dest='command',
        help='Available commands',
        metavar='COMMAND'
    )

    subparsers.add_parser(
        'init',
        help='Initialize a git repository inside a Perforce workspace',
        description='Set up a new git repository inside a Perforce workspace. '
        'Checks that it runs in a Perforce workspace, asks for the depot root, '
        'writable mode, split users and the Claude Code skill, then sets up '
        '.gitignore, initializes git and creates a first commit.'
    ).set_defaults(func=init_command, needs_workspace=False)

    sync_parser = subparsers.add_parser(
        'sync',
        help='Sync the git repository with Perforce',
        description='Sync the Perforce workspace to one or more changelists, '
        'with a git commit for each. Local changes to synced files are merged '
        'back afterwards.'
    )
    sync_parser.set_defaults(func=sync_command)
    sync_parser.add_argument(
        '--bookmark',
        help='jj bookmark to move to each sync commit, when the parent of the jj working change has several'
    )
    sync_parser.add_argument(
        'changelist',
        nargs='*',
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
        help='Allow syncing to changelists older than the current one'
    )
    _add_dry_run(
        sync_parser, 'Print the resolved sync sequence without syncing')
    sync_parser.add_argument(
        '--ignore-blocking-processes',
        action='store_true',
        help='Sync even while a configured blocking process is running'
    )
    sync_parser.add_argument(
        '-u', '--split-user',
        action='append',
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

    split_users_parser = subparsers.add_parser(
        'sync-split-users',
        help='Show or edit the users whose changelists sync splits out',
        description='Split users are the Perforce users whose submitted '
        'changelists sync gives a git commit each, holding nothing but that '
        'change. Without an action, lists them.'
    )
    split_users_parser.set_defaults(
        func=sync_split_users_command, snapshot=False)
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

    new_parser = subparsers.add_parser(
        'new',
        help='Create a changelist, open files for edit, and optionally create a Swarm review',
        description='Create a Perforce changelist with a description and the '
        'enumerated git commits since the base branch, and save its number '
        'under an alias. By default also opens the changed files for edit in '
        'the changelist.'
    )
    new_parser.set_defaults(func=new_command)
    _add_message_args(
        new_parser,
        'Changelist description message. Defaults to the HEAD commit subject, '
        'or with the jj backend the jj change description',
        'Read the changelist description message from the given file',
        from_head=True)
    new_parser.add_argument(
        '--no-commit-list',
        action='store_true',
        help='Leave the enumerated commit list out of the changelist description'
    )
    _add_base_branch(
        new_parser, 'Base branch for enumerating commits and finding changed files')
    _add_branch_keyword_arg(new_parser, 'alias',
                            'Alias name to save the new changelist number under')
    new_parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Overwrite an existing alias'
    )
    new_parser.add_argument(
        '--no-alias',
        action='store_true',
        help='Skip saving a changelist alias'
    )
    _add_dry_run(
        new_parser, 'Pretend and print what would be done, but do not execute')
    _add_no_edit(new_parser)
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

    update_parser = subparsers.add_parser(
        'update',
        help='Update an existing changelist description and open files for edit',
        description='Update an existing Perforce changelist description: '
        'commits since the base branch replace their existing entries in '
        'the enumerated commit list and new ones are appended; entries '
        'outside the range are kept. The message above the list is kept '
        'unless -m or -F replaces it. By default also opens changed files '
        'for edit.'
    )
    update_parser.set_defaults(func=update_command)
    _add_branch_keyword_arg(update_parser, 'changelist',
                            'Changelist number or named alias to update')
    _add_message_args(
        update_parser,
        'Replace the changelist description message above the commit list',
        'Replace the changelist description message above the commit list '
        'with the contents of the given file')
    _add_base_branch(
        update_parser, 'Base branch for enumerating commits and finding changed files')
    _add_dry_run(
        update_parser, 'Pretend and print what would be done, but do not execute')
    update_parser.add_argument(
        '--no-commit-list',
        action='store_true',
        help='Leave the enumerated commit list in the changelist description as it is'
    )
    _add_no_edit(update_parser)
    update_parser.add_argument(
        '--shelve',
        action='store_true',
        help='Re-shelve the changelist after updating'
    )
    update_parser.add_argument(
        '--per-commit',
        action='store_true',
        help='Update the changelist once per commit since the base branch, '
             'oldest first, with each commit checked out in turn. With --shelve '
             'each commit gets a shelf of its own'
    )

    list_changes_parser = subparsers.add_parser(
        'list-changes',
        help='List the commit subjects since the base branch',
        description='List the commit subjects since the base branch as a '
        'numbered list, oldest first, as new puts them in a changelist '
        'description.'
    )
    list_changes_parser.set_defaults(func=list_changes_command)
    _add_base_branch(list_changes_parser, 'Base branch to compare against')

    alias_parser = subparsers.add_parser(
        'alias',
        help='Manage changelist aliases',
        description='Manage changelist aliases: names for changelist numbers, '
        'stored in .git-p4son/aliases/.'
    )
    # alias reads no file of the change, at most the tip's bookmark, so jj need not snapshot.
    alias_parser.set_defaults(snapshot=False)
    alias_subparsers = alias_parser.add_subparsers(
        dest='alias_action',
        help='Available alias actions',
        metavar='ACTION'
    )

    alias_subparsers.add_parser(
        'list',
        help='List all aliases and their changelist numbers',
        description='List all changelist aliases with their changelist '
        'numbers.'
    ).set_defaults(func=alias_list_command)

    alias_show_parser = alias_subparsers.add_parser(
        'show',
        help='Print the changelist number of an alias',
        description='Print only the changelist number saved under an alias, '
        'for use in scripts.'
    )
    # It prints only the changelist number on stdout, so scripts can capture it.
    alias_show_parser.set_defaults(func=alias_show_command, quiet=True)
    _add_branch_keyword_arg(alias_show_parser, 'alias',
                            'Alias name to show')

    alias_new_parser = alias_subparsers.add_parser(
        'new',
        help='Save a changelist number under a named alias',
        description='Save a changelist number under a named alias in '
        '.git-p4son/aliases/<alias>.toml.'
    )
    alias_new_parser.set_defaults(func=alias_new_command)
    alias_new_parser.add_argument(
        'changelist',
        help='Changelist number to save'
    )
    _add_branch_keyword_arg(alias_new_parser, 'alias',
                            'Alias name to save the changelist number under')
    alias_new_parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Overwrite an existing alias'
    )

    alias_delete_parser = alias_subparsers.add_parser(
        'delete',
        help='Delete a changelist alias',
        description='Delete a changelist alias, or all of them with --all.'
    )
    alias_delete_parser.set_defaults(func=alias_delete_command)
    alias_delete_target = alias_delete_parser.add_mutually_exclusive_group()
    _add_branch_keyword_arg(alias_delete_parser, 'alias',
                            'Alias name to delete', group=alias_delete_target)
    alias_delete_target.add_argument(
        '--all',
        action='store_true',
        help='Delete all aliases without prompting'
    )

    alias_subparsers.add_parser(
        'clean',
        help='Clean up changelist aliases',
        description='List all changelist aliases, then delete all of them '
        'or review each one interactively with yes/no/all/quit prompts.'
    ).set_defaults(func=alias_clean_command)

    review_parser = subparsers.add_parser(
        'review',
        help='Create a Swarm review, one shelf per commit',
        description='Create a Swarm review from the commits since the base '
        'branch, oldest first: check out the first and run git p4son new '
        '--review on it, then check out each later one and run git p4son '
        'update --shelve, before returning to where you started.'
    )
    review_parser.set_defaults(func=review_command)
    _add_branch_keyword_arg(review_parser, 'alias',
                            'Alias name for the new changelist')
    _add_message_args(
        review_parser,
        'Changelist description message. Defaults to the HEAD commit subject, '
        'or with the jj backend the jj change description',
        'Read the changelist description message from the given file',
        from_head=True)
    _add_base_branch(
        review_parser, 'Base branch to find commits since')
    review_parser.add_argument(
        '-f', '--force',
        action='store_true',
        help='Overwrite an existing alias'
    )
    _add_dry_run(
        review_parser, 'Print the steps, one per commit, without running them')
    review_parser.add_argument(
        '--no-commit-list',
        action='store_true',
        help='Leave the enumerated commit list out of the changelist description. '
             'Passed on to new and each update step'
    )

    writable_parser = subparsers.add_parser(
        'writable',
        help='Show or apply writable mode for git-tracked files',
        description='Writable mode keeps git-tracked files writable so they '
        'can be edited without a manual p4 edit. Git-ignored files are left '
        'to Perforce. Without an action, shows whether the mode is on.'
    )
    # Without an action, writable only shows the setting; the actions read the change.
    writable_parser.set_defaults(func=writable_command, snapshot=False)
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
    ).set_defaults(snapshot=True)
    writable_subparsers.add_parser(
        'disable',
        help='Turn writable mode off and make git-tracked files read-only',
        description='Turn writable mode off, then make git-tracked files '
        'read-only, except files opened in Perforce.'
    ).set_defaults(snapshot=True)
    writable_subparsers.add_parser(
        'apply',
        help='Make git-tracked files match the writable mode',
        description='With writable mode on, make every git-tracked file '
        'writable. With it off, make them read-only, except files opened in '
        'Perforce. Use it to fix files made read-only outside git-p4son, '
        'for example by a submit.'
    ).set_defaults(snapshot=True)

    completion_parser = subparsers.add_parser(
        'completion',
        help='Print the path to a shell completion script',
        description='Print the path to a shell completion script '
        'for bash, zsh, or powershell.'
    )
    completion_parser.set_defaults(
        func=completion_command, needs_workspace=False)
    completion_parser.add_argument(
        'shell',
        choices=['bash', 'zsh', 'powershell'],
        help='Shell to print the completion script path for'
    )
    completion_parser.add_argument(
        '-d', '--dirname',
        action='store_true',
        help='Print the directory instead of the full file path'
    )

    skill_parser = subparsers.add_parser(
        'skill',
        help='Install or show the Claude Code skill for git-p4son',
        description='Manage the Claude Code skill that teaches AI agents how '
        'to use git-p4son. The installed skill only tells the agent to run '
        '"git p4son skill show", so it stays current when git-p4son is '
        'upgraded.'
    )
    skill_parser.set_defaults(needs_workspace=False)
    skill_subparsers = skill_parser.add_subparsers(
        dest='skill_action',
        help='Available skill actions',
        metavar='ACTION'
    )
    skill_subparsers.add_parser(
        'install',
        help='Install the skill for Claude Code',
        description='Write the skill to ~/.claude/skills/git-p4son/SKILL.md '
        '(or under $CLAUDE_CONFIG_DIR), replacing any existing one.'
    ).set_defaults(func=skill_install_command)
    skill_subparsers.add_parser(
        'show',
        help='Print the instructions the skill gives to AI agents',
        description='Print the instructions for AI agents that ship with '
        'this version of git-p4son, followed by the workspace\'s own '
        'overrides in .git-p4son/skill-override.md, if it has any.'
    ).set_defaults(func=skill_show_command)

    return parser


_COMPLETION_FILES = {
    'bash': 'git-p4son.bash',
    'zsh': '_git-p4son',
    'powershell': 'git-p4son.ps1',
}


def completion_command(args: argparse.Namespace) -> int:
    filename = _COMPLETION_FILES[args.shell]
    completions_dir = files('git_p4son') / 'completions'

    if args.dirname:
        print(completions_dir)
    else:
        print(completions_dir / filename)

    return 0


def run_command(args: argparse.Namespace) -> int:
    with log.quiet(getattr(args, 'quiet', False)):
        return _run_command(args)


def _run_command(args: argparse.Namespace) -> int:
    """run_command's body, inside its quiet scope: find the workspace, open the backend and run the command."""
    args.invocation_dir = os.getcwd()

    if not hasattr(args, 'func'):
        # Only a command whose actions name the handlers, given no action, gets here.
        raise CommandError(f'No {args.command} action specified. '
                           f'Use "git-p4son {args.command} -h" for help.')

    if not getattr(args, 'needs_workspace', True):
        return _run_handler(args)

    log.heading('Finding workspace directory')
    args.workspace_dir = get_workspace_dir()
    if not args.workspace_dir:
        raise CommandError(
            'No .git directory found in the current or parent directories. '
            'Run "git p4son init" first.')
    log.success(args.workspace_dir)

    snapshot = getattr(args, 'snapshot', True)
    checkouts = args.command == 'review' or getattr(args, 'per_commit', False)
    with open_backend(args.workspace_dir, args.backend_choice, snapshot=snapshot,
                      checkouts=checkouts) as backend:
        args.backend = backend
        exit_code = _run_in_workspace(args)
    if exit_code == 0 and getattr(args, 'dry_run', False):
        log.info('Dry run, nothing changed')
    return exit_code


def _run_in_workspace(args: argparse.Namespace) -> int:
    """Resolve the arguments that depend on the workspace, then run the command."""
    backend = args.backend
    if hasattr(args, 'base_branch'):
        args.base_branch = backend.resolve_base(args.base_branch)

    if getattr(args, 'no_alias', False):
        args.alias = None

    branch_attr = getattr(args, 'branch_keyword_arg', None)
    if (branch_attr and getattr(args, branch_attr) == 'branch'
            and not getattr(args, 'all', False)):
        words = backend.words
        log.heading(
            f'Resolving alias from current {words.tool} {words.branch}')
        resolved = backend.suggested_alias()
        setattr(args, branch_attr, resolved)
        log.success(f'branch -> {resolved}')

    if getattr(args, 'file', None) is not None:
        args.file = os.path.join(args.invocation_dir, args.file)
        log.heading('Reading message from file')
        try:
            with open(args.file, encoding='utf-8') as f:
                args.message = f.read().strip()
        except OSError as e:
            raise CommandError(f'Failed to read message file: {e}') from e
        if not args.message:
            raise CommandError(f'Message file {args.file} is empty')
        log.success(args.file)

    if getattr(args, 'message_from_head', False) and args.message is None:
        log.heading(f'Resolving message from {backend.words.message_source}')
        args.message = backend.subject()
        log.success(args.message)

    return _run_handler(args)


def _run_handler(args: argparse.Namespace) -> int:
    """Run the command's handler."""
    # set_defaults(func=...) leaves the handler untyped; every handler returns an exit code.
    return cast(int, args.func(args))


def main() -> int:
    # Handle 'complete' before argparse to avoid flag/word conflicts
    if len(sys.argv) >= 2 and sys.argv[1] == 'complete':
        words = sys.argv[2:]
        if words and words[0] == '--':
            words = words[1:]
        return run_complete(words)

    parser = create_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    try:
        log.verbose_mode = args.verbose

        return run_command(args)
    except KeyboardInterrupt:
        log.error('\nOperation cancelled by user')
        return 1
    except RunError as e:
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
