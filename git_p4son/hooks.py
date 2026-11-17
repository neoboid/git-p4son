"""Hooks: the commands a workspace configures in [hooks] in config.toml, run at set points of a sync."""

import os
from typing import Any

from .common import run
from .config import ConfigError, load_config
from .configured_command import PERFORCE_VARIABLES, SYNC_VARIABLES, check_command, check_keys, command_line, \
    sync_values
from .hooks_migration import check_no_old_hooks
from .log import log, plural

SECTION = 'hooks'
HOOKS = ('pre-sync', 'post-sync')
_BUILT_IN = {
    'pre-sync': ('backend', *SYNC_VARIABLES, *PERFORCE_VARIABLES),
    'post-sync': ('backend', 'synced', *SYNC_VARIABLES, *PERFORCE_VARIABLES),
}


def _section(workspace_dir: str) -> dict[str, Any]:
    section = load_config(workspace_dir).get(SECTION, {})
    if not isinstance(section, dict):
        raise ConfigError(f'{SECTION} in config.toml must be a table')
    check_keys(section, HOOKS, SECTION)
    return section


def hook_commands(hook: str, workspace_dir: str) -> list[list[str]]:
    """The commands of hook, pre-sync or post-sync, in [hooks] in config.toml, in order.

    Raises ConfigError for a [hooks] section or a command that isn't valid."""
    value = _section(workspace_dir).get(hook, [])
    setting = f'{SECTION}.{hook}'
    if not isinstance(value, list):
        raise ConfigError(f'{setting} in config.toml must be a list of commands, each a list of strings, one per '
                          'argument')
    if value and all(isinstance(item, str) for item in value):
        raise ConfigError(f'{setting} in config.toml must be a list of commands: write a single command as '
                          '[["program", "argument", ...]]')
    return [check_command(command, _BUILT_IN[hook], f'command {number} of {setting}')
            for number, command in enumerate(value, 1)]


def run_hook(hook: str, workspace_dir: str, backend: str, last_synced: int | None, depot_root: str,
             synced: int | None = None) -> int:
    """Run hook's commands in order from the workspace root; the number that failed.

    backend, git or jj, last_synced, depot_root and, for post-sync, synced fill in their variables."""
    commands = hook_commands(hook, workspace_dir)
    if not commands:
        return 0
    log.heading(f'Running {hook} hooks')
    values = {'backend': backend, **sync_values(last_synced, depot_root)}
    if synced is not None:
        values['synced'] = str(synced)
    root = os.path.abspath(workspace_dir)
    failed = 0
    for number, arguments in enumerate(commands, 1):
        name = f'Hook command {number} of {SECTION}.{hook}'
        command = command_line(arguments, values, workspace_dir)
        # Hooks are independent, so one failing does not stop the rest.
        try:
            result = run(command, cwd=root, fail_on_returncode=False)
        except OSError as error:
            log.error(f'{name} could not start {command[0]}: {error.strerror}')
            failed += 1
            continue
        for line in result.stdout:
            log.output(line)
        for line in result.stderr:
            log.output(line, stderr=True)
        if result.returncode != 0:
            log.error(f'{name} failed with return code {result.returncode}')
            failed += 1
    if failed:
        log.warning(f'{failed} of {plural(len(commands), "hook")} failed')
    else:
        log.success(f'{plural(len(commands), "hook")} ran')
    return failed


def check_hooks(workspace_dir: str) -> None:
    """Raise CommandError for hooks that are not valid, or set up as in earlier versions (check_no_old_hooks)."""
    check_no_old_hooks(workspace_dir)
    for hook in HOOKS:
        hook_commands(hook, workspace_dir)
