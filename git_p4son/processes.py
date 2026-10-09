"""Blocking processes: a sync is refused while one of them is running."""

import csv
import os

from .common import RunError, run
from .config import load_config
from .log import log

IGNORE_FLAG = '--ignore-blocking-processes'


def get_blocking_processes(workspace_dir: str) -> list[str]:
    """Return the configured blocking process names; raise ValueError if malformed."""
    sync = load_config(workspace_dir).get('sync', {})
    if not isinstance(sync, dict):
        raise ValueError('sync in config.toml is not a table')
    names = sync.get('blocking-processes', [])
    if (not isinstance(names, list)
            or not all(isinstance(name, str) for name in names)):
        raise ValueError(
            'sync.blocking-processes in config.toml must be a list of strings')
    return names


def normalize_process_name(name: str) -> str:
    """Reduce a process name to a case-insensitive basename without .exe."""
    name = os.path.basename(name.strip())
    if name.lower().endswith('.exe'):
        name = name[:-len('.exe')]
    return name.lower()


def get_running_processes() -> set[str]:
    """Return the normalized names of all currently running processes."""
    if os.name == 'nt':
        # /NH: no header row. CSV quotes names containing spaces or commas.
        result = run(['tasklist', '/NH', '/FO', 'CSV'])
        lines = [line for line in result.stdout if line.strip()]
        return {normalize_process_name(row[0])
                for row in csv.reader(lines) if row}
    # -ww: don't truncate long process names to the terminal width.
    result = run(['ps', '-A', '-ww', '-o', 'comm='])
    return {normalize_process_name(line)
            for line in result.stdout if line.strip()}


def check_no_blocking_processes(workspace_dir: str, ignore: bool) -> bool:
    """Report whether the sync may go ahead; unreadable config or process list fails the check."""
    if ignore:
        log.heading('Checking for blocking processes')
        log.warning(f'Skipped ({IGNORE_FLAG})')
        return True

    try:
        names = get_blocking_processes(workspace_dir)
    except ValueError as error:
        log.error(f'Could not read the blocking processes: {error}')
        return False
    if not names:
        return True

    log.heading('Checking for blocking processes')
    try:
        running = get_running_processes()
    except (OSError, RunError) as error:
        log.error(f'Could not list running processes: {error}')
        return False

    blocking = sorted(name for name in names
                      if normalize_process_name(name) in running)
    if not blocking:
        log.success('None running')
        return True

    for name in blocking:
        log.info(name)
    log.error('Refusing to sync while these blocking processes are running. '
              f'Close them and sync again, or pass {IGNORE_FLAG}')
    return False
