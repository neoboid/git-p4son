"""User hook discovery and execution."""

import os
from pathlib import Path

from . import CONFIG_DIR
from .common import RunResult, run
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
