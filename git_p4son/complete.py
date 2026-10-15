"""Shell completion for git-p4son, introspecting the argparse parser."""

import argparse

from .changelist_store import branch_alias, list_changelist_aliases
from .common import CommandError
from .git import get_current_branch, get_workspace_dir
from .log import log
from . import jj
from .sync_split_users import USER_PLACEHOLDER, get_split_users

_HIDDEN_COMMANDS = frozenset(
    {'complete', 'completion', '_sequence-editor'})


def _get_subparsers_action(parser):
    """Extract the _SubParsersAction from a parser, if any."""
    if parser._subparsers is None:
        return None
    for action in parser._subparsers._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _flag_takes_value(action):
    """Check if an argparse action consumes a value argument."""
    return not isinstance(action, (
        argparse._StoreTrueAction,
        argparse._StoreFalseAction,
        argparse._StoreConstAction,
        argparse._CountAction,
        argparse._HelpAction,
        argparse._VersionAction,
    ))


def _get_flags(parser):
    """Get all optional flags from a parser as (flag, help) pairs."""
    flags = []
    for action in parser._actions:
        if not action.option_strings:
            continue
        if isinstance(action, argparse._HelpAction):
            continue
        help_text = action.help or ''
        if help_text == argparse.SUPPRESS:
            continue
        for opt in action.option_strings:
            flags.append((opt, help_text))
    return flags


def _find_flag_action(parser, flag):
    """Find the action for a given flag string."""
    for action in parser._actions:
        if flag in action.option_strings:
            return action
    return None


def _get_alias_names(workspace_dir):
    """Get alias names for completion."""
    if not workspace_dir:
        return []
    try:
        return [(name, f'CL {cl}')
                for name, cl in list_changelist_aliases(workspace_dir)]
    except Exception:
        return []


def _get_split_user_names(workspace_dir):
    """Get configured split users for completion, leaving out $(user) (the shell would run it)."""
    if not workspace_dir:
        return []
    try:
        return [(name, 'Split user')
                for name in get_split_users(workspace_dir)
                if name != USER_PLACEHOLDER]
    except Exception:
        return []


def _filter(candidates, prefix):
    """Filter candidates by prefix and return matches."""
    return [(name, description) for name, description in candidates
            if name.startswith(prefix)]


def _complete_flag_value(flag, workspace_dir=None, ignore_jj=False):
    """Complete the value for a flag."""
    if flag in ('-b', '--base-branch'):
        if workspace_dir and not ignore_jj and jj.is_workspace(workspace_dir):
            try:
                workspace = jj.Workspace.load(workspace_dir, '@')
            except (CommandError, OSError):
                return []
            return [(name, 'jj bookmark') for name in workspace.bookmarks]
        return [('__branch__', '')]
    return []


def _get_branch_candidates(prefix, workspace_dir, ignore_jj=False):
    """Get 'branch' completion candidates based on prefix."""
    keyword = 'branch'
    if not workspace_dir or not keyword.startswith(prefix):
        return []
    use_jj = not ignore_jj and jj.is_workspace(workspace_dir)
    try:
        branch = (jj.Workspace.load(workspace_dir, '@').current_bookmark()
                  if use_jj else get_current_branch(workspace_dir))
    except (CommandError, OSError):
        return []
    alias = branch_alias(branch) if branch else None
    if alias is None:
        return []
    if prefix == keyword:
        return [(alias, 'Current bookmark' if use_jj else 'Current branch')]
    return [(keyword, 'Use current bookmark name' if use_jj else 'Use current branch name')]


def _complete_positional(command, subcommand, positional_count,
                         prefix, workspace_dir, command_parser,
                         subcommand_parser, actions, ignore_jj=False):
    """Complete a positional argument."""
    # Arguments added with the branch keyword name themselves in branch_keyword_arg.
    target = subcommand_parser or command_parser
    dest = target.get_default('branch_keyword_arg')
    positionals = [a for a in target._actions if not a.option_strings
                   and not isinstance(a, argparse._SubParsersAction)]
    if dest and positional_count < len(positionals) \
            and positionals[positional_count].dest == dest:
        return (_get_branch_candidates(prefix, workspace_dir, ignore_jj)
                + _filter(_get_alias_names(workspace_dir), prefix))

    if command == 'sync':
        if positional_count == 0:
            candidates = [
                ('head', 'Sync to the latest changelist'),
                ('last-synced', 'Re-sync last synced changelist'),
            ]
            return _filter(candidates, prefix)
        # "head" may also close out a sequence of changelist numbers.
        return _filter(
            [('head', 'Sync to the latest changelist')], prefix)

    # Commands with nested actions (alias list, ...) take the action first.
    if actions and subcommand is None and positional_count == 0:
        candidates = [(ca.dest, ca.help or '')
                      for ca in actions._choices_actions]
        return _filter(candidates, prefix)

    if command == 'sync-split-users' and subcommand == 'delete':
        return _filter(_get_split_user_names(workspace_dir), prefix)

    return []


def _complete(parser, words, workspace_dir=None):
    """Return (name, description) completion candidates for the given words."""
    if not words:
        words = ['']

    prefix = words[-1]
    preceding = words[:-1]

    subparsers_action = _get_subparsers_action(parser)
    if not subparsers_action:
        return []

    command = None
    command_parser = None
    actions = None
    subcommand = None
    subcommand_parser = None
    positional_count = 0
    expecting_flag_name = None
    current_parser = parser

    for word in preceding:
        if expecting_flag_name is not None:
            expecting_flag_name = None
            continue

        if word.startswith('-'):
            action = _find_flag_action(current_parser, word)
            if action and _flag_takes_value(action):
                expecting_flag_name = word
            continue

        if command is None:
            if word in subparsers_action.choices:
                command = word
                command_parser = subparsers_action.choices[word]
                actions = _get_subparsers_action(command_parser)
                current_parser = command_parser
            continue

        if actions and subcommand is None and word in actions.choices:
            subcommand = word
            subcommand_parser = actions.choices[word]
            current_parser = subcommand_parser
            continue

        positional_count += 1

    if expecting_flag_name is not None:
        return _complete_flag_value(expecting_flag_name, workspace_dir,
                                    '--ignore-jj' in preceding)

    if command is None:
        if prefix.startswith('-'):
            return _filter(_get_flags(parser), prefix)
        else:
            candidates = []
            for ca in subparsers_action._choices_actions:
                if ca.dest not in _HIDDEN_COMMANDS:
                    candidates.append((ca.dest, ca.help or ''))
            return _filter(candidates, prefix)

    if prefix.startswith('-'):
        return _filter(_get_flags(current_parser), prefix)

    return _complete_positional(command, subcommand, positional_count,
                                prefix, workspace_dir, command_parser,
                                subcommand_parser, actions, '--ignore-jj' in preceding)


def run_complete(words):
    """Execute the complete command with the given word list."""
    from .cli import create_parser
    # stdout is reserved for completion candidates, so silence all logging.
    log.quiet_mode = True
    parser = create_parser()
    workspace_dir = get_workspace_dir()
    candidates = _complete(parser, words, workspace_dir)
    for name, description in candidates:
        if description:
            print(f'{name}\t{description}')
        else:
            print(name)
    return 0
