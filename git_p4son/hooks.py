"""User hook discovery and execution."""

import os
from pathlib import Path

from . import CONFIG_DIR
from .common import RunResult, run
from .config import load_config
from .log import log

DEFAULT_WINDOWS_ASSOCIATIONS: dict[str, list[str]] = {
    '.ps1': [
        'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File'
    ],
    '.nu': ['nushell.exe'],
    '.sh': ['bash.exe'],
    '.py': ['python.exe'],
}


def _hook_dir(workspace_dir: str, hook_name: str) -> Path:
    """Return the directory containing scripts for a named hook."""
    return Path(workspace_dir) / CONFIG_DIR / 'hooks' / hook_name


def _configured_windows_associations(workspace_dir: str) -> dict[str, list[str]]:
    """Return Windows extension associations from config."""
    config = load_config(workspace_dir)
    associations = config.get('hooks', {}).get('extension-associations', {})
    result: dict[str, list[str]] = {}

    for extension, command in associations.items():
        normalized_extension = extension.lower()
        if not normalized_extension.startswith('.'):
            normalized_extension = f'.{normalized_extension}'

        if isinstance(command, str):
            result[normalized_extension] = [command]
        elif isinstance(command, list) and all(
                isinstance(part, str) for part in command):
            result[normalized_extension] = command
        else:
            log.warning(
                'Ignoring invalid hook extension association for '
                f'{extension}')

    return result


def _windows_associations(workspace_dir: str) -> dict[str, list[str]]:
    """Return default Windows associations overridden by config."""
    associations = DEFAULT_WINDOWS_ASSOCIATIONS.copy()
    associations.update(_configured_windows_associations(workspace_dir))
    return associations


def _is_windows() -> bool:
    """Return True when running on Windows."""
    return os.name == 'nt'


def _hook_command(path: Path, workspace_dir: str) -> list[str] | None:
    """Return the command for a hook file, or None if not executable."""
    if _is_windows():
        association = _windows_associations(workspace_dir).get(
            path.suffix.lower())
        if association is None:
            return None
        return [*association, str(path)]

    if not os.access(path, os.X_OK):
        return None
    return [str(path)]


def _print_stdout(result: RunResult) -> None:
    """Print stdout from a completed hook."""
    for line in result.stdout:
        print(line)


def run_hooks(hook_name: str, workspace_dir: str,
              invocation_dir: str) -> list[RunResult]:
    """Run all executable files for a named hook."""
    hook_dir = _hook_dir(workspace_dir, hook_name)
    if not hook_dir.is_dir():
        return []

    entries = sorted(hook_dir.iterdir(), key=lambda entry: entry.name)
    files = [entry for entry in entries if entry.is_file()]
    if not files:
        return []

    log.heading(f'Running {hook_name} hooks')

    results: list[RunResult] = []
    repo_root = os.path.abspath(workspace_dir)
    cwd = os.path.abspath(invocation_dir)
    env = {'GIT_P4SON_REPO_ROOT_DIR': repo_root}

    for path in files:
        command = _hook_command(path, workspace_dir)
        display_path = os.path.relpath(path, workspace_dir)
        if command is None:
            log.warning(f'Skipping non-executable hook: {display_path}')
            continue

        result = run(command, cwd=cwd, env=env)
        _print_stdout(result)
        results.append(result)

    return results
