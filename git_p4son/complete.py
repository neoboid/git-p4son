"""Shell completion for git-p4son, introspecting the argparse parser."""

import argparse
from typing import TypeAlias

from .backend import BACKENDS, Backend, alias_for_branch, completion_backend
from .changelist_store import list_changelist_aliases
from .common import CommandError
from .git import get_workspace_dir
from .log import log
from .sync_split_users import USER_PLACEHOLDER, get_split_users

_HIDDEN_COMMANDS = frozenset({'complete', 'completion'})

# A completion candidate: (name, description).
Candidates = list[tuple[str, str]]
# Generic only to type checkers, so quoted.
Subparsers: TypeAlias = 'argparse._SubParsersAction[argparse.ArgumentParser]'


def _get_subparsers_action(parser: argparse.ArgumentParser) -> 'Subparsers | None':
    """Extract the _SubParsersAction from a parser, if any."""
    if parser._subparsers is None:
        return None
    for action in parser._subparsers._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _flag_takes_value(action: argparse.Action) -> bool:
    """Check if an argparse action consumes a value argument."""
    return not isinstance(action, (
        argparse._StoreTrueAction,
        argparse._StoreFalseAction,
        argparse._StoreConstAction,
        argparse._CountAction,
        argparse._HelpAction,
        argparse._VersionAction,
    ))


def _get_flags(parser: argparse.ArgumentParser) -> Candidates:
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


def _find_flag_action(parser: argparse.ArgumentParser, flag: str) -> argparse.Action | None:
    """Find the action for a given flag string."""
    for action in parser._actions:
        if flag in action.option_strings:
            return action
    return None


def _get_alias_names(workspace_dir: str | None) -> Candidates:
    """Get alias names for completion."""
    if not workspace_dir:
        return []
    try:
        return [(name, f'CL {cl}')
                for name, cl in list_changelist_aliases(workspace_dir)]
    except Exception:
        return []


def _get_split_user_names(workspace_dir: str | None) -> Candidates:
    """Get configured split users for completion, leaving out $(user) (the shell would run it)."""
    if not workspace_dir:
        return []
    try:
        return [(name, 'Split user')
                for name in get_split_users(workspace_dir)
                if name != USER_PLACEHOLDER]
    except Exception:
        return []


def _filter(candidates: Candidates, prefix: str) -> Candidates:
    """Filter candidates by prefix and return matches."""
    return [(name, description) for name, description in candidates
            if name.startswith(prefix)]


def _backend(workspace_dir: str | None, backend_choice: str | None) -> Backend | None:
    """The backend for workspace_dir, or None when there is none or jj cannot be read."""
    if not workspace_dir:
        return None
    try:
        return completion_backend(workspace_dir, backend_choice)
    except (CommandError, OSError):
        return None


def _complete_flag_value(flag: str, workspace_dir: str | None = None,
                         backend_choice: str | None = None) -> Candidates:
    """Complete the value for a flag."""
    if flag == '--backend':
        return [(name, f'Use the {name} backend') for name in BACKENDS]
    if flag in ('-b', '--base-branch', '--branch'):
        if not workspace_dir:
            return [('__branch__', '')]
        backend = _backend(workspace_dir, backend_choice)
        return backend.base_candidates() if backend else []
    return []


def _get_branch_candidates(prefix: str, workspace_dir: str | None,
                           backend_choice: str | None = None, new_alias: bool = False) -> Candidates:
    """Get 'branch' completion candidates based on prefix."""
    keyword = 'branch'
    if not keyword.startswith(prefix):
        return []
    backend = _backend(workspace_dir, backend_choice)
    if backend is None:
        return []
    try:
        alias = alias_for_branch(backend, str(workspace_dir), new_alias)
    except (CommandError, OSError):
        return []
    branch = backend.words.branch
    if prefix == keyword:
        return [(alias, f'Current {branch}')]
    return [(keyword, f'Use current {branch} name')]


def _complete_positional(command: str, subcommand: str | None, positional_count: int,
                         prefix: str, workspace_dir: str | None,
                         command_parser: argparse.ArgumentParser,
                         subcommand_parser: argparse.ArgumentParser | None,
                         actions: 'Subparsers | None', backend_choice: str | None = None) -> Candidates:
    """Complete a positional argument."""
    # Arguments added with the branch keyword name themselves in branch_keyword_arg.
    target = subcommand_parser or command_parser
    dest = target.get_default('branch_keyword_arg')
    positionals = [a for a in target._actions if not a.option_strings
                   and not isinstance(a, argparse._SubParsersAction)]
    if dest and positional_count < len(positionals) \
            and positionals[positional_count].dest == dest:
        new_alias = bool(target.get_default('new_alias'))
        return (_get_branch_candidates(prefix, workspace_dir, backend_choice, new_alias)
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


def _complete(parser: argparse.ArgumentParser, words: list[str],
              workspace_dir: str | None = None) -> Candidates:
    """Return (name, description) completion candidates for the given words."""
    if not words:
        words = ['']

    prefix = words[-1]
    preceding = words[:-1]

    subparsers_action = _get_subparsers_action(parser)
    if not subparsers_action:
        return []

    command: str | None = None
    command_parser: argparse.ArgumentParser | None = None
    actions: Subparsers | None = None
    subcommand: str | None = None
    subcommand_parser: argparse.ArgumentParser | None = None
    positional_count = 0
    expecting_flag_name: str | None = None
    backend_choice: str | None = None
    current_parser = parser

    for word in preceding:
        if expecting_flag_name is not None:
            if expecting_flag_name == '--backend' and word in BACKENDS:
                backend_choice = word
            expecting_flag_name = None
            continue

        if word.startswith('--backend='):
            value = word.removeprefix('--backend=')
            backend_choice = value if value in BACKENDS else backend_choice
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
        return _complete_flag_value(expecting_flag_name, workspace_dir, backend_choice)

    if command is None or command_parser is None:
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
                                subcommand_parser, actions, backend_choice)


def run_complete(words: list[str]) -> int:
    """Execute the complete command with the given word list."""
    from .cli import create_parser
    # stdout is reserved for completion candidates, so silence all logging.
    with log.quiet():
        candidates = _complete(create_parser(), words, get_workspace_dir())
    for name, description in candidates:
        if description:
            print(f'{name}\t{description}')
        else:
            print(name)
    return 0
