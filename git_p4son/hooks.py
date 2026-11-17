"""Hooks: the commands a workspace configures in [hooks] in config.toml, run at set points of a sync."""

import os
from pathlib import Path
from typing import Any

from . import CONFIG_DIR
from .common import RunResult, run
from .config import ConfigError, load_config
from .configured_command import PERFORCE_VARIABLES, SYNC_VARIABLES, check_command, check_keys, command_line, \
    sync_values
from .hooks_migration import check_no_old_hooks
from .log import log, plural
from .scripts import script_command, script_runners


def _hook_dir(workspace_dir: str, hook_name: str) -> Path:
    """Return the directory containing scripts for a named hook."""
    return Path(workspace_dir) / CONFIG_DIR / 'hooks' / hook_name


def run_hooks(hook_name: str, workspace_dir: str, invocation_dir: str,
              backend: str) -> list[RunResult[list[str]]]:
    """Run all executable files for a named hook, telling them the backend, git or jj, in GIT_P4SON_BACKEND."""
    hook_dir = _hook_dir(workspace_dir, hook_name)
    if not hook_dir.is_dir():
        return []

    entries = sorted(hook_dir.iterdir(), key=lambda entry: entry.name)
    files = [entry for entry in entries if entry.is_file()]
    if not files:
        return []

    log.heading(f'Running {hook_name} hooks')

    results: list[RunResult[list[str]]] = []
    repo_root = os.path.abspath(workspace_dir)
    cwd = os.path.abspath(invocation_dir)
    env = {'GIT_P4SON_REPO_ROOT_DIR': repo_root, 'GIT_P4SON_BACKEND': backend}

    runners = script_runners(workspace_dir)
    for path in files:
        command = script_command(path, runners)
        display_path = os.path.relpath(path, workspace_dir)
        if command is None:
            log.warning(f'Skipping non-executable hook: {display_path}')
            continue

        # Hooks are independent, so one failing does not stop the rest.
        result = run(command, cwd=cwd, env=env, fail_on_returncode=False)
        for line in result.stdout:
            log.output(line)
        for line in result.stderr:
            log.output(line, stderr=True)
        if result.returncode != 0:
            log.error(
                f'Hook {display_path} failed '
                f'with return code {result.returncode}')
        results.append(result)

    failed = sum(1 for result in results if result.returncode != 0)
    if failed:
        log.warning(f'{failed} of {plural(len(results), "hook")} failed')
    else:
        log.success(f'{plural(len(results), "hook")} ran')
    return results


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
