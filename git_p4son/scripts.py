"""The command that runs a workspace script: the file itself on POSIX, by extension on Windows."""

import os
from pathlib import Path

from .config import get_setting
from .log import log

DEFAULT_WINDOWS_RUNNERS: dict[str, list[str]] = {
    '.ps1': [
        'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File'
    ],
    '.sh': ['bash.exe'],
    '.py': ['python.exe'],
}

# Read in this order, so core wins; hooks.extension-associations is the setting's old name, still read.
_RUNNER_SETTINGS = (('hooks', 'extension-associations'),
                    ('core', 'script-runners'))


def _is_windows() -> bool:
    """Return True when running on Windows."""
    return os.name == 'nt'


def _runner(setting: str, extension: str, command: object) -> tuple[str, list[str]] | None:
    """The normalized extension and its runner, or None with a warning for an invalid entry."""
    if isinstance(command, str):
        command = [command]
    elif not (isinstance(command, list) and all(isinstance(part, str) for part in command)):
        log.warning(
            f'Ignoring invalid script runner for {extension} in {setting}')
        return None
    return (extension.lower() if extension.startswith('.') else f'.{extension.lower()}'), command


def script_runners(workspace_dir: str) -> dict[str, list[str]]:
    """The command that runs each script extension on Windows, the defaults overridden by config; empty elsewhere.

    Raises ConfigError when a setting is not a table."""
    if not _is_windows():
        return {}
    runners = DEFAULT_WINDOWS_RUNNERS.copy()
    for section, key in _RUNNER_SETTINGS:
        configured = get_setting(workspace_dir, section, key, dict, {})
        for extension, command in configured.items():
            entry = _runner(f'{section}.{key}', extension, command)
            if entry is not None:
                runners[entry[0]] = entry[1]
    return runners


def script_command(path: Path, runners: dict[str, list[str]]) -> list[str] | None:
    """Return the command that runs the script at path, given script_runners, or None if it can't run."""
    if _is_windows():
        runner = runners.get(path.suffix.lower())
        if runner is None:
            return None
        return [*runner, str(path)]

    if not os.access(path, os.X_OK):
        return None
    return [str(path)]
