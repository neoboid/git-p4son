"""
Shell completion for git-p4son.

Provides completion candidates by introspecting the argparse parser.
Output format: one candidate per line, with optional tab-separated description.
Special directives (e.g. __branch__) tell the shell wrapper to use native completion.
"""

import argparse

from .changelist_store import list_changelist_aliases
from .common import branch_to_alias, get_current_branch, get_workspace_dir

_HIDDEN_COMMANDS = frozenset({'complete', '_sequence-editor'})


def _get_subparsers_action(parser):
    """Extract the _SubParsersAction from a parser, if any."""
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


def _filter(candidates, prefix):
    """Filter candidates by prefix and return matches."""
    return [(name, description) for name, description in candidates
            if name.startswith(prefix)]


def _complete_flag_value(flag, prefix):
    """Complete the value for a flag."""
    if flag in ('-b', '--base-branch'):
        return [('__branch__', '')]
    return []


def _get_branch_candidates(prefix, workspace_dir):
    """Get @branch completion candidates based on prefix."""
    if not workspace_dir:
        return []
    keyword = '@branch'
    if prefix == keyword:
        branch = get_current_branch(workspace_dir)
        if branch and branch != 'main':
            return [(branch_to_alias(branch), 'Current branch')]
        return []
    if keyword.startswith(prefix):
        branch = get_current_branch(workspace_dir)
        if branch and branch != 'main':
            return [(keyword, 'Use current branch name')]
    return []


def _complete_positional(command, subcommand, positional_count,
                         prefix, workspace_dir, command_parser):
    """Complete a positional argument."""
    aliases = _get_alias_names(workspace_dir)

    if command == 'sync' and positional_count == 0:
        candidates = [
            ('latest', 'Sync to latest changelist'),
            ('last-synced', 'Re-sync last synced changelist'),
        ] + aliases
        return _filter(candidates, prefix)

    if command == 'update' and positional_count == 0:
        return _filter(aliases, prefix)

    if command == 'alias':
        if subcommand is None and positional_count == 0:
            nested = _get_subparsers_action(command_parser)
            if nested and hasattr(nested, '_choices_actions'):
                candidates = [(ca.dest, ca.help or '')
                              for ca in nested._choices_actions]
                return _filter(candidates, prefix)
            return []

        if subcommand == 'delete' and positional_count == 0:
            return _filter(aliases, prefix)

        if subcommand == 'set' and positional_count == 1:
            return _filter(aliases, prefix)

    if command in ('new', 'review') and positional_count == 0:
        branch_candidates = _get_branch_candidates(prefix, workspace_dir)
        return branch_candidates + _filter(aliases, prefix)

    return []


def _complete(parser, words, workspace_dir=None):
    """Generate completion candidates for the given words.

    Returns a list of (name, description) tuples.
    """
    if not words:
        words = ['']

    prefix = words[-1]
    preceding = words[:-1]

    subparsers_action = _get_subparsers_action(parser)
    if not subparsers_action:
        return []

    command = None
    command_parser = None
    subcommand = None
    subcommand_parser = None
    positional_count = 0
    expecting_flag_value = False
    expecting_flag_name = None
    current_parser = parser

    for word in preceding:
        if expecting_flag_value:
            expecting_flag_value = False
            expecting_flag_name = None
            continue

        if word.startswith('-'):
            action = _find_flag_action(current_parser, word)
            if action and _flag_takes_value(action):
                expecting_flag_value = True
                expecting_flag_name = word
            continue

        if command is None:
            if word in subparsers_action.choices:
                command = word
                command_parser = subparsers_action.choices[word]
                current_parser = command_parser
            continue

        if command == 'alias' and subcommand is None:
            nested = _get_subparsers_action(command_parser)
            if nested and word in nested.choices:
                subcommand = word
                subcommand_parser = nested.choices[word]
                current_parser = subcommand_parser
                continue

        positional_count += 1

    # Completing a flag's value
    if expecting_flag_value:
        return _complete_flag_value(expecting_flag_name, prefix)

    # Completing the command name
    if command is None:
        if prefix.startswith('-'):
            return _filter(_get_flags(parser), prefix)
        else:
            candidates = []
            for ca in getattr(subparsers_action, '_choices_actions', []):
                if ca.dest not in _HIDDEN_COMMANDS:
                    candidates.append((ca.dest, ca.help or ''))
            return _filter(candidates, prefix)

    # Completing a flag for the current command
    if prefix.startswith('-'):
        return _filter(_get_flags(current_parser), prefix)

    # Completing a positional argument
    return _complete_positional(command, subcommand, positional_count,
                                prefix, workspace_dir, command_parser)


def run_complete(words):
    """Execute the complete command with the given word list."""
    from .cli import create_parser
    parser = create_parser()
    workspace_dir = get_workspace_dir()
    candidates = _complete(parser, words, workspace_dir)
    for name, description in candidates:
        if description:
            print(f'{name}\t{description}')
        else:
            print(name)
    return 0
