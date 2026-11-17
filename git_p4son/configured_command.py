"""Command lines a workspace configures in config.toml: their arguments, $(name) variables and program."""

import os
import re
from collections.abc import Callable

from .common import CommandError
from .config import ConfigError
from .perforce import get_client_spec, get_p4_port, get_p4_user

_VARIABLE = re.compile(r'\$\(([\w-]+)\)')


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
PERFORCE_VARIABLES = tuple(_PERFORCE_VARIABLES)
SYNC_VARIABLES = ('last-synced', 'depot-root')


def sync_values(last_synced: int | None, depot_root: str) -> dict[str, str]:
    """The values of SYNC_VARIABLES: the last synced changelist, empty before the first sync, and the depot root."""
    return {'last-synced': str(last_synced or ''), 'depot-root': depot_root}


def _check_arguments(value: object, setting: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(
            f'{setting} in config.toml must be a list of strings, one per argument')
    return value


def _check_variables(arguments: list[str], built_in: tuple[str, ...], setting: str) -> None:
    for name in sorted({name for argument in arguments for name in _VARIABLE.findall(argument)}):
        if name not in built_in and name not in os.environ:
            raise ConfigError(f'{setting} in config.toml uses $({name}), which is neither one of '
                              f'{", ".join(built_in)} nor an environment variable')


def check_keys(table: dict[str, object], keys: tuple[str, ...], setting: str) -> None:
    """Raise ConfigError for a key of table, the setting named setting, that isn't one of keys."""
    unknown = sorted(set(table) - set(keys))
    if unknown:
        raise ConfigError(f'{setting} in config.toml has unknown keys: {", ".join(unknown)}. '
                          f'The keys are: {", ".join(keys)}')


def check_command(value: object, built_in: tuple[str, ...], setting: str) -> list[str]:
    """value, the setting that configures a command as its list of arguments.

    Raises ConfigError for a value that isn't a non-empty list of strings, or a $(name) that is neither built in
    nor an environment variable."""
    command = _check_arguments(value, setting)
    if not command:
        raise ConfigError(f'{setting} in config.toml is empty')
    _check_variables(command, built_in, setting)
    return command


def check_command_table(config: dict[str, object], keys: tuple[str, ...], built_in: tuple[str, ...],
                        table: str) -> dict[str, list[str]]:
    """config, the table that configures a command with keys, each a list of arguments, command among them.

    Raises ConfigError for an unknown key, a missing or empty command, an argument list that isn't a list of
    strings, or a $(name) that is neither built in nor an environment variable."""
    check_keys(config, keys, table)
    if 'command' not in config:
        raise ConfigError(f'{table} in config.toml has no command')
    arguments = {key: _check_arguments(
        value, f'{key} in {table}') for key, value in config.items()}
    if not arguments['command']:
        raise ConfigError(f'command in {table} in config.toml is empty')
    _check_variables([item for value in arguments.values()
                     for item in value], built_in, table)
    return arguments


def _expand_variables(arguments: list[str], values: dict[str, str], workspace_dir: str) -> list[str]:
    """Replace each $(name) in the arguments: from values, a Perforce value, else an environment variable."""
    values = dict(values)

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in values:
            lookup = _PERFORCE_VARIABLES.get(name)
            values[name] = lookup(
                workspace_dir) if lookup else os.environ[name]
        return values[name]
    return [_VARIABLE.sub(value, argument) for argument in arguments]


def _from_workspace_root(command: list[str], workspace_dir: str) -> list[str]:
    """command with a relative program path joined to the workspace root; a bare name is left to PATH."""
    if os.path.dirname(command[0]) and not os.path.isabs(command[0]):
        # Windows looks for the program from git-p4son's own directory, not the cwd it is given.
        return [os.path.normpath(os.path.join(workspace_dir, command[0])), *command[1:]]
    return command


def command_line(arguments: list[str], values: dict[str, str], workspace_dir: str) -> list[str]:
    """The command line arguments configure, its variables expanded and its program found from the workspace root."""
    return _from_workspace_root(_expand_variables(arguments, values, workspace_dir), workspace_dir)
