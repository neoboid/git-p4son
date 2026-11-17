"""The hook setup of earlier versions, files in .git-p4son/hooks/<hook>/ run by extension, refused with its fix."""

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from . import CONFIG_DIR
from .common import CommandError
from .config import ConfigError, load_config
from .log import log
from .toml_writer import format_value

_SECTION = 'hooks'
_HOOKS = ('pre-sync', 'post-sync')
_RUNNER_SETTINGS = ((_SECTION, 'extension-associations'),
                    ('core', 'script-runners'))
_DEFAULT_RUNNERS: dict[str, list[str]] = {
    '.ps1': ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File'],
    '.sh': ['bash.exe'],
    '.py': ['python.exe'],
}


def _is_windows() -> bool:
    return sys.platform == 'win32'


def _runner_settings(config: dict[str, Any]) -> Iterator[tuple[str, object]]:
    """Each script runner setting config holds, by name, with its value."""
    for section, key in _RUNNER_SETTINGS:
        table = config.get(section, {})
        if isinstance(table, dict) and key in table:
            yield f'{section}.{key}', table[key]


def _runners(config: dict[str, Any]) -> dict[str, list[str]]:
    """The interpreters hook files ran with on Windows, by extension, as configured where the setting was valid."""
    runners = dict(_DEFAULT_RUNNERS)
    for _, configured in _runner_settings(config):
        for extension, command in (configured.items() if isinstance(configured, dict) else ()):
            command = [command] if isinstance(command, str) else command
            if isinstance(command, list) and all(isinstance(part, str) for part in command):
                runners['.' + extension.lower().lstrip('.')] = command
    return runners


def _command(path: Path, relative: str, runners: dict[str, list[str]]) -> list[str] | None:
    """The command that ran the hook file at path, naming it as relative, or None if none did."""
    if _is_windows():
        runner = runners.get(path.suffix.lower())
        return None if runner is None else [*runner, relative]
    return [relative] if os.access(path, os.X_OK) else None


def _named(config: dict[str, Any], hook: str, workspace_dir: str) -> set[str]:
    """The paths, normalized, that the arguments of hook's configured commands could name; unchecked."""
    section = config.get(_SECTION, {})
    value = section.get(hook, []) if isinstance(section, dict) else []
    arguments = [argument for command in (value if isinstance(value, list) else []) if isinstance(command, list)
                 for argument in command if isinstance(argument, str)]
    return {os.path.normcase(os.path.normpath(os.path.join(workspace_dir, argument))) for argument in arguments}


def _check_no_hook_files(config: dict[str, Any], workspace_dir: str) -> None:
    """Raise CommandError, after printing the [hooks] lines that run them, for hook files no command names.

    A file that never ran as a hook, such as an editor's backup, is only warned about."""
    runners = _runners(config)
    commands: dict[str, list[list[str]]] = {}
    never_ran: list[str] = []
    for hook in _HOOKS:
        directory = Path(workspace_dir) / CONFIG_DIR / 'hooks' / hook
        if not directory.is_dir():
            continue
        named = _named(config, hook, workspace_dir)
        for path in sorted((entry for entry in directory.iterdir() if entry.is_file()), key=lambda e: e.name):
            if os.path.normcase(os.path.normpath(path)) in named:
                continue
            relative = path.relative_to(workspace_dir).as_posix()
            command = _command(path, relative, runners)
            if command is None:
                never_ran.append(relative)
            else:
                commands.setdefault(hook, []).append(command)
    for relative in never_ran:
        log.warning(f'Ignoring {relative}, which never ran as a hook')
    if not commands:
        return
    section = config.get(_SECTION)
    # A table that holds only [hooks.extension-associations] has no [hooks] header to add the keys under.
    if isinstance(section, dict) and any(hook in section for hook in _HOOKS):
        log.info(f'Hook files in {CONFIG_DIR}/hooks/ no longer run on their own. Set these keys in the '
                 f'[{_SECTION}] table of {CONFIG_DIR}/config.toml:')
    else:
        log.info(f'Hook files in {CONFIG_DIR}/hooks/ no longer run on their own. Run them from '
                 f'{CONFIG_DIR}/config.toml:')
        log.info(f'[{_SECTION}]')
    for hook, listed in commands.items():
        configured = section.get(hook, []) if isinstance(section, dict) else []
        log.info(f'{hook} = [')
        for command in [*(configured if isinstance(configured, list) else []), *listed]:
            log.info(f'  {format_value(command)},')
        log.info(']')
    log.info('Hooks no longer get GIT_P4SON_BACKEND or GIT_P4SON_REPO_ROOT_DIR: pass $(backend), and the working '
             'directory is the root.')
    raise CommandError(f'Hooks are configured in [{_SECTION}] in {CONFIG_DIR}/config.toml now, as lists of '
                       'commands. See the lines above.')


def check_no_old_hooks(workspace_dir: str) -> None:
    """Raise CommandError for the hook setup of earlier versions.

    That is a hook file no [hooks] command names, after printing the [hooks] lines that run it with the interpreter
    it ran with, or, once there is none, a script runner setting. A file that never ran is only warned about."""
    config = load_config(workspace_dir)
    _check_no_hook_files(config, workspace_dir)
    for setting, _ in _runner_settings(config):
        raise ConfigError(f'{setting} in config.toml is no longer read: a hook command names its interpreter, as in '
                          '["python", "tools/hook.py"]. Remove it.')
