"""Writable mode: keeps git-tracked files writable and opens them in Perforce on demand."""

import argparse
import os
import stat
import time
from datetime import timedelta
from typing import Callable

from .common import CommandError
from .config import get_setting, save_config
from .git import list_tracked_files
from .jj import change_revision
from .log import log, plural
from .perforce import get_client_spec, opened_lookup, p4_get_opened_files


def is_writable_mode(workspace_dir: str) -> bool:
    """Whether writable mode is on: git-tracked files are kept writable; raise ConfigError if malformed."""
    return get_setting(workspace_dir, 'core', 'writable', bool, False)


def set_writable_mode(workspace_dir: str, enabled: bool) -> None:
    """Turn writable mode on or off in the config."""
    save_config(workspace_dir, {'core': {'writable': enabled}})


def _regular_file_mode(path: str) -> int | None:
    """Permission bits of path if it is a regular file (symlinks not followed), else None."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return stat.S_IMODE(st.st_mode)


def is_user_writable(path: str) -> bool:
    """Whether path is a regular file (symlinks not followed) with user write permission."""
    mode = _regular_file_mode(path)
    return mode is not None and bool(mode & stat.S_IWUSR)


def _set_user_write(paths: list[str], writable: bool) -> int:
    """Set or clear user write permission on each regular file; returns how many changed."""
    changed = 0
    for path in paths:
        mode = _regular_file_mode(path)
        if mode is None:
            continue
        new_mode = mode | stat.S_IWUSR if writable else mode & ~stat.S_IWUSR
        if new_mode != mode:
            os.chmod(path, new_mode)
            changed += 1
    return changed


def make_writable(paths: list[str]) -> int:
    """Add user write permission to each regular file lacking it; returns how many changed."""
    return _set_user_write(paths, True)


def make_read_only(paths: list[str]) -> int:
    """Remove user write permission from each regular file having it; returns how many changed."""
    return _set_user_write(paths, False)


def restore_writable(paths: list[str], tracked: set[str], workspace_dir: str) -> int:
    """Make the paths in tracked writable again after p4 wrote them; returns how many changed."""
    return make_writable([os.path.join(workspace_dir, path)
                          for path in paths if path in tracked])


def _tracked_paths(workspace_dir: str, revision: str | None,
                   skip: Callable[[str], object] = lambda rel: False) -> list[str]:
    """Every file tracked by git, in the index or else in revision, that skip leaves in, as absolute paths."""
    return [os.path.join(workspace_dir, rel)
            for rel in list_tracked_files(workspace_dir, revision) if not skip(rel)]


def _change_modes(action: Callable[[list[str]], int], paths: list[str]) -> None:
    start = time.monotonic()
    changed = action(paths)
    log.elapsed(timedelta(seconds=time.monotonic() - start))
    log.success(f'{changed} of {plural(len(paths), "tracked file")} changed')


def _apply_on(workspace_dir: str, revision: str | None) -> None:
    """Make every tracked file writable."""
    log.heading('Making git-tracked files writable')
    _change_modes(make_writable, _tracked_paths(workspace_dir, revision))


def _apply_off(workspace_dir: str, revision: str | None) -> None:
    """Make tracked files read-only, except those opened in Perforce."""
    log.heading('Checking Perforce workspace')
    spec = get_client_spec(workspace_dir)
    if not spec:
        raise CommandError('Not inside a Perforce workspace. '
                           'Is Perforce installed and configured?')
    log.success(spec.name)
    if spec.allwrite:
        # p4 keeps allwrite files writable, so making them read-only would fight it.
        log.warning('The workspace has the allwrite option, '
                    'leaving tracked files writable')
        return

    # Opened files keep their write bit. The whole client is queried, not just the depot root.
    log.heading('Checking files opened in Perforce')
    opened = dict(p4_get_opened_files(f'//{spec.name}', workspace_dir))
    log.success(f'{plural(len(opened), "file")} opened')

    log.heading('Making git-tracked files read-only')
    _change_modes(make_read_only,
                  _tracked_paths(workspace_dir, revision, opened_lookup(opened)))


def writable_command(args: argparse.Namespace) -> int:
    """Execute the writable command."""
    workspace_dir = args.workspace_dir

    if args.writable_action is None:
        log.heading('Writable mode')
        log.success('on' if is_writable_mode(workspace_dir) else 'off')
        return 0

    if args.writable_action == 'apply':
        enabled = is_writable_mode(workspace_dir)
    else:
        enabled = args.writable_action == 'enable'
        log.heading('Setting writable mode')
        set_writable_mode(workspace_dir, enabled)
        log.success('on' if enabled else 'off')
    revision = change_revision(args)
    if enabled:
        _apply_on(workspace_dir, revision)
    else:
        _apply_off(workspace_dir, revision)
    return 0
