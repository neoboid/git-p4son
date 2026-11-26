"""Writable mode: keeps git-tracked files writable and opens them in Perforce on demand."""

import argparse
import os
import stat
import time
from datetime import timedelta
from typing import Callable

from .common import CommandError
from .config import get_setting, set_setting_in_place
from .git import list_tracked_files
from .log import log, plural
from .perforce import P4ClientSpec, get_client_spec, opened_lookup, p4_get_opened_files


def check_not_allwrite(spec: P4ClientSpec | None) -> None:
    """Raise CommandError for a client spec with the allwrite option, which git-p4son does not support."""
    if spec and spec.allwrite:
        raise CommandError('The workspace\'s client spec has the allwrite option, which git-p4son does not support. '
                           'Change it to noallwrite with "p4 client", then run the command again. To keep tracked '
                           'files writable, use "git p4son writable enable".')


def is_writable_mode(workspace_dir: str) -> bool:
    """Whether writable mode is on: git-tracked files are kept writable; raise ConfigError if malformed."""
    return get_setting(workspace_dir, 'core', 'writable', bool, False)


def set_writable_mode(workspace_dir: str, enabled: bool) -> None:
    """Turn writable mode on or off in the config."""
    set_setting_in_place(workspace_dir, 'core', 'writable', enabled)


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


def _tracked_paths(workspace_dir: str, revision: str,
                   skip: Callable[[str], object] = lambda rel: False) -> list[str]:
    """Every file tracked in revision that skip leaves in, as absolute paths."""
    return [os.path.join(workspace_dir, rel)
            for rel in list_tracked_files(workspace_dir, revision) if not skip(rel)]


def _change_modes(paths: list[str], writable: bool) -> None:
    start = time.monotonic()
    changed = _set_user_write(paths, writable)
    log.elapsed(timedelta(seconds=time.monotonic() - start))
    outcome = 'made writable' if writable else 'made read-only'
    log.success(f'{changed} of {plural(len(paths), "tracked file")} {outcome}')


def _apply_on(workspace_dir: str, revision: str) -> None:
    """Make every tracked file writable."""
    log.heading('Making git-tracked files writable')
    _change_modes(_tracked_paths(workspace_dir, revision), True)


def _checked_client_spec(workspace_dir: str) -> P4ClientSpec:
    """The workspace's client spec, under a heading.

    Raises CommandError outside a Perforce workspace, and for a client spec with the allwrite option."""
    log.heading('Checking Perforce workspace')
    spec = get_client_spec(workspace_dir)
    if not spec:
        raise CommandError('Not inside a Perforce workspace. '
                           'Is Perforce installed and configured?')
    log.success(spec.name)
    check_not_allwrite(spec)
    return spec


def _apply_off(spec: P4ClientSpec, workspace_dir: str, revision: str) -> None:
    """Make tracked files read-only, except those opened in Perforce."""
    # Opened files keep their write bit. The whole client is queried, not just the depot root.
    log.heading('Checking files opened in Perforce')
    opened = dict(p4_get_opened_files(f'//{spec.name}', workspace_dir))
    log.success(f'{plural(len(opened), "file")} opened')

    log.heading('Making git-tracked files read-only')
    paths = _tracked_paths(workspace_dir, revision, opened_lookup(opened))
    _change_modes(paths, False)


def writable_command(args: argparse.Namespace) -> int:
    """Execute the writable command."""
    workspace_dir = args.workspace_dir

    if args.writable_action is None:
        log.heading('Writable mode')
        log.success('on' if is_writable_mode(workspace_dir) else 'off')
        return 0

    enabled = is_writable_mode(
        workspace_dir) if args.writable_action == 'apply' else args.writable_action == 'enable'
    # The off path reads the client spec first, so an allwrite workspace is refused before the setting changes.
    spec = None if enabled else _checked_client_spec(workspace_dir)
    if args.writable_action != 'apply':
        log.heading('Setting writable mode')
        set_writable_mode(workspace_dir, enabled)
        log.success('on' if enabled else 'off')
    revision = args.backend.revision
    if spec is None:
        _apply_on(workspace_dir, revision)
    else:
        _apply_off(spec, workspace_dir, revision)
    return 0
