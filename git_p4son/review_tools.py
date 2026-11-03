"""Review tools: Swarm, built in, and the command lines the workspace configures in [review-tool.<name>]."""

import os
import re
from collections.abc import Callable
from typing import Any

from .changelist_store import mark_review_started
from .common import CommandError, run_interactive
from .config import ConfigError, load_config
from .log import log
from .perforce import get_client_spec, get_p4_port, get_p4_user

SWARM = 'swarm'

# Seconds to give Swarm to process a shelf before the next step shelves again.
SWARM_SHELF_DELAY = 5

_SECTION = 'review-tool'
_DEFAULT = 'default'
_KEYS = ('command', 'new', 'update')
_VARIABLE = re.compile(r'\$\((\w+)\)')


def _workspace(workspace_dir: str) -> str:
    spec = get_client_spec(workspace_dir)
    if spec is None:
        raise CommandError('No Perforce workspace for $(workspace)')
    return spec.name


def _user(workspace_dir: str) -> str:
    user = get_p4_user(workspace_dir)
    if user is None:
        raise CommandError('No Perforce user for $(user)')
    return user


_PERFORCE_VARIABLES: dict[str, Callable[[str], str]] = {
    'workspace': _workspace,
    'user': _user,
    'p4port': get_p4_port,
}
_BUILT_IN = ('changelist', *_PERFORCE_VARIABLES)


def _section(workspace_dir: str) -> dict[str, Any]:
    section = load_config(workspace_dir).get(_SECTION, {})
    if not isinstance(section, dict):
        raise ConfigError(f'{_SECTION} in config.toml is not a table')
    unknown = sorted(key for key, value in section.items()
                     if key != _DEFAULT and not isinstance(value, dict))
    if unknown:
        raise ConfigError(f'[{_SECTION}] in config.toml has unknown keys: {", ".join(unknown)}. '
                          f'The keys are: {_DEFAULT}, and a table per tool')
    for name, reason in ((SWARM, 'swarm is built in'), (_DEFAULT, f'{_DEFAULT} names the default tool')):
        if isinstance(section.get(name), dict):
            raise ConfigError(
                f'[{_SECTION}.{name}] in config.toml can\'t be a review tool, as {reason}. Rename it.')
    return section


def review_tool_names(workspace_dir: str) -> list[str]:
    """The review tools a workspace can use, by name: swarm, then the tables under [review-tool]."""
    return [SWARM, *sorted(name for name, value in _section(workspace_dir).items() if isinstance(value, dict))]


def review_tool_config(tool: str, workspace_dir: str) -> dict[str, list[str]]:
    """The tool's command and its new and update arguments.

    Raises CommandError when the tool is not configured, or its configuration is not valid."""
    config = _section(workspace_dir).get(tool)
    if not isinstance(config, dict):
        raise CommandError(f'Review tool {tool} is not configured. The review tools are: '
                           f'{", ".join(review_tool_names(workspace_dir))}. Configure it again, or to push the review '
                           'to another tool, delete the CL and its alias and create the review again.')
    table = f'[{_SECTION}.{tool}]'
    unknown = sorted(set(config) - set(_KEYS))
    if unknown:
        raise ConfigError(f'{table} in config.toml has unknown keys: {", ".join(unknown)}. '
                          f'The keys are: {", ".join(_KEYS)}')
    if 'command' not in config:
        raise ConfigError(f'{table} in config.toml has no command')
    for key, value in config.items():
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ConfigError(
                f'{key} in {table} in config.toml must be a list of strings, one per argument')
    if not config['command']:
        raise ConfigError(f'command in {table} in config.toml is empty')
    for name in sorted({name for value in config.values() for item in value for name in _VARIABLE.findall(item)}):
        if name not in _BUILT_IN and name not in os.environ:
            raise ConfigError(f'{table} in config.toml uses $({name}), which is neither one of '
                              f'{", ".join(_BUILT_IN)} nor an environment variable')
    return config


def resolve_review_tool(name: str | None, workspace_dir: str) -> str | None:
    """The review tool to run after each shelve: the one named, else [review-tool] default; None for Swarm.

    Raises CommandError for a tool that isn't configured or whose configuration isn't valid."""
    section = _section(workspace_dir)
    default = section.get(_DEFAULT, SWARM)
    if not isinstance(default, str):
        raise ConfigError(
            f'{_SECTION}.{_DEFAULT} in config.toml must be a string')
    tool = name or default
    tools = review_tool_names(workspace_dir)
    if tool not in tools:
        source = '--review-tool' if name else f'{_SECTION}.{_DEFAULT} in config.toml'
        raise CommandError(
            f'Unknown review tool "{tool}" ({source}). The review tools are: {", ".join(tools)}')
    if tool == SWARM:
        return None
    review_tool_config(tool, workspace_dir)
    return tool


def shelf_delay(tool: str | None) -> int | None:
    """Seconds to sleep between the shelves of a per-commit run: Swarm's delay, none for a tool that is run."""
    return SWARM_SHELF_DELAY if tool is None else None


def _expand(arguments: list[str], changelist: str, workspace_dir: str) -> list[str]:
    """Replace each $(name) in the arguments: the changelist, a Perforce value, else an environment variable."""
    values = {'changelist': changelist}

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            lookup = _PERFORCE_VARIABLES.get(name)
            values[name] = lookup(
                workspace_dir) if lookup else os.environ[name]
        return values[name]
    return [_VARIABLE.sub(value, argument) for argument in arguments]


def run_review_tool(tool: str, changelist: str, *, first: bool, workspace_dir: str, dry_run: bool) -> None:
    """Run the review tool's command with its new or update arguments from the workspace root, after a shelve.

    The first run creates the review, until one succeeds. Raises CommandError when the tool fails or can't be run."""
    log.heading(f'Running review tool {tool}')
    config = review_tool_config(tool, workspace_dir)
    if dry_run:
        target = f'CL {changelist}' if changelist.isdigit() else 'the new CL'
        log.success(f'Would run review tool {tool} for {target}')
        return
    command = _expand([*config['command'], *config.get('new' if first else 'update', [])],
                      changelist, workspace_dir)
    if os.path.dirname(command[0]) and not os.path.isabs(command[0]):
        # Windows looks for the program from git-p4son's own directory, not the cwd it is given.
        command[0] = os.path.normpath(os.path.join(workspace_dir, command[0]))
    try:
        result = run_interactive(command, workspace_dir)
    except OSError as error:
        raise CommandError(f'Review tool {tool} could not start {command[0]}: {error.strerror}. '
                           f'Check command in [{_SECTION}.{tool}] in config.toml.') from error
    if result.returncode != 0:
        raise CommandError(
            f'Review tool {tool} failed for CL {changelist}, with exit code {result.returncode}')
    log.success(f'Ran review tool {tool} for CL {changelist}')


def push_review(alias: str, tool: str, changelist: str, *, first: bool, workspace_dir: str, dry_run: bool) -> None:
    """Run the review tool after a shelve, then record in the alias that the tool has run for the CL."""
    run_review_tool(tool, changelist, first=first,
                    workspace_dir=workspace_dir, dry_run=dry_run)
    if not dry_run:
        mark_review_started(alias, workspace_dir)
