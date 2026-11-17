"""Sync targets: names for the changelist a command the workspace configures in [sync.targets] prints."""

import re

from .changelist_store import validate_alias_name
from .common import CommandError, run_for_output
from .config import ConfigError, get_setting
from .configured_command import PERFORCE_VARIABLES, SYNC_VARIABLES, check_command, command_line, sync_values
from .log import log

_SECTION = 'sync'
_TARGETS = 'targets'
_DEFAULT = 'default-target'
_BUILT_IN = (*SYNC_VARIABLES, *PERFORCE_VARIABLES)
_CHANGELIST = re.compile(r'[0-9]+')


def _setting(name: str) -> str:
    return f'{_SECTION}.{_TARGETS}.{name}'


def _parses_as_number(name: str) -> bool:
    """Whether sync reads name as a changelist number, as int() does."""
    try:
        int(name)
    except ValueError:
        return False
    return True


def _command(name: str, value: object) -> list[str]:
    """The command of target name, configured as value; raise ConfigError unless both are valid."""
    problem = validate_alias_name(name, 'sync target')
    if problem is None and name != name.lower():
        problem = f'Sync target name "{name}" must be lowercase'
    if problem is None and _parses_as_number(name):
        problem = f'Sync target name "{name}" would be taken as a changelist number'
    if problem is not None:
        raise ConfigError(f'{_setting(name)} in config.toml: {problem}')
    if isinstance(value, dict):
        raise ConfigError(f'{_setting(name)} in config.toml is a table: write a target as '
                          'name = ["program", "argument", ...], quoting a name with a dot')
    return check_command(value, _BUILT_IN, _setting(name))


def _sync_targets(workspace_dir: str) -> dict[str, list[str]]:
    """Each sync target's command, by name; raise ConfigError for one that isn't valid."""
    section = get_setting(workspace_dir, _SECTION, _TARGETS, dict, {})
    return {name: _command(name, value) for name, value in section.items()}


def sync_target_names(workspace_dir: str) -> list[str]:
    """The configured sync targets, sorted.

    Raises ConfigError for a target that isn't valid."""
    return sorted(_sync_targets(workspace_dir))


def default_sync_target(workspace_dir: str) -> str | None:
    """[sync] default-target, the target a sync without changelists resolves, or None.

    Raises ConfigError when it names no sync target."""
    name = get_setting(workspace_dir, _SECTION, _DEFAULT, str, None)
    if name is None:
        return None
    names = sync_target_names(workspace_dir)
    if name.lower() not in names:
        listed = ', '.join(names) or 'none'
        raise ConfigError(f'{_SECTION}.{_DEFAULT} in config.toml is "{name}", which is not a sync target. '
                          f'The sync targets are: {listed}')
    return name.lower()


def resolve_sync_target(argument: str, last_synced: int | None, depot_root: str, workspace_dir: str) -> int:
    """The changelist the command of sync target argument prints, run from the workspace root.

    Raises CommandError when argument is no sync target, or its command fails or prints anything but a number."""
    name = argument.lower()
    targets = _sync_targets(workspace_dir)
    if name not in targets:
        listed = (f'. The sync targets are: {", ".join(sorted(targets))}' if targets
                  else f', and no sync targets are configured in [{_SECTION}.{_TARGETS}] in config.toml')
        raise CommandError(
            f'"{argument}" is neither a changelist number nor a sync target{listed}')
    log.heading(f'Resolving sync target {name}')
    command = command_line(targets[name], sync_values(
        last_synced, depot_root), workspace_dir)
    try:
        result = run_for_output(command, workspace_dir)
    except OSError as error:
        raise CommandError(f'Sync target {name} could not start {command[0]}: {error.strerror}. '
                           f'Check {_setting(name)} in config.toml.') from error
    if result.returncode != 0:
        raise CommandError(
            f'Sync target {name} failed, with exit code {result.returncode}')
    output = '\n'.join(result.stdout).strip()
    if not _CHANGELIST.fullmatch(output):
        for line in result.stdout:
            log.info(line)
        printed = 'the output above' if output else 'nothing'
        raise CommandError(
            f'Sync target {name} printed {printed}, not a changelist number')
    changelist = int(output)
    log.success(f'CL {changelist}')
    return changelist
