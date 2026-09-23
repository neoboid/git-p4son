"""
Writable mode for git-p4son.

Writable mode keeps git-tracked files writable so they can be edited without a
manual p4 edit: git-p4son opens them in Perforce itself when creating or
updating changelists. Git-ignored files are left to Perforce as usual.
"""

import argparse
import os
import stat
import time
from datetime import timedelta

from .config import load_config, save_config
from .git import list_tracked_files
from .log import log
from .perforce import get_client_spec, p4_get_opened_files


def is_writable_mode(workspace_dir: str) -> bool:
    """Whether writable mode is on: git-tracked files are kept writable."""
    config = load_config(workspace_dir)
    return config.get('core', {}).get('writable') is True


def set_writable_mode(workspace_dir: str, enabled: bool) -> None:
    """Turn writable mode on or off in the config."""
    save_config(workspace_dir, {'core': {'writable': enabled}})


def _regular_file_mode(path: str) -> int | None:
    """Permission bits of path if it is a regular file, else None.

    lstat, not stat: a symlink is never followed, so a link pointing outside
    the repo cannot get its target's permissions changed."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return stat.S_IMODE(st.st_mode)


def make_writable(paths: list[str]) -> int:
    """Add user write permission to each regular file that lacks it.

    Symlinks, directories and missing paths are skipped. Returns how many
    files were changed."""
    changed = 0
    for path in paths:
        mode = _regular_file_mode(path)
        if mode is None or mode & stat.S_IWUSR:
            continue
        os.chmod(path, mode | stat.S_IWUSR)
        changed += 1
    return changed


def make_read_only(paths: list[str]) -> int:
    """Remove user write permission from each regular file that has it.

    Symlinks, directories and missing paths are skipped. Returns how many
    files were changed."""
    changed = 0
    for path in paths:
        mode = _regular_file_mode(path)
        if mode is None or not mode & stat.S_IWUSR:
            continue
        os.chmod(path, mode & ~stat.S_IWUSR)
        changed += 1
    return changed


def _tracked_paths(workspace_dir: str) -> list[str]:
    """Every file tracked by git, as absolute paths."""
    return [os.path.join(workspace_dir, rel)
            for rel in list_tracked_files(workspace_dir)]


def _timed(action, paths: list[str]) -> int:
    """Run action over paths, log the elapsed time, and return its count."""
    start = time.monotonic()
    changed = action(paths)
    log.elapsed(timedelta(seconds=time.monotonic() - start))
    return changed


def _apply_on(workspace_dir: str) -> int:
    """Make every tracked file writable."""
    log.heading('Making git-tracked files writable')
    paths = _tracked_paths(workspace_dir)
    changed = _timed(make_writable, paths)
    log.success(f'{changed} of {len(paths)} tracked files changed')
    return 0


def _apply_off(workspace_dir: str) -> int:
    """Make tracked files read-only, except those opened in Perforce."""
    log.heading('Checking Perforce workspace')
    spec = get_client_spec(workspace_dir)
    if not spec:
        log.error('Not inside a Perforce workspace')
        return 1
    log.success(spec.name)
    if spec.allwrite:
        # The client spec asks for every file to be writable, and p4 keeps
        # them that way on sync, so making them read-only would fight it.
        log.warning('The workspace has the allwrite option, '
                    'leaving tracked files writable')
        return 0

    # A file opened in any changelist is being worked on through Perforce
    # (for example by new or update), so it keeps its write bit. The whole
    # client is queried, not just the depot root, since a tracked file can
    # be opened anywhere in the workspace.
    log.heading('Finding files opened in Perforce')
    opened = {os.path.normcase(path) for path, _change in
              p4_get_opened_files(f'//{spec.name}', workspace_dir)}
    log.success(f'{len(opened)} opened')

    log.heading('Making git-tracked files read-only')
    paths = [os.path.join(workspace_dir, rel)
             for rel in list_tracked_files(workspace_dir)
             if os.path.normcase(rel) not in opened]
    changed = _timed(make_read_only, paths)
    log.success(f'{changed} of {len(paths)} tracked files changed')
    return 0


def apply_writable_mode(workspace_dir: str) -> int:
    """Make tracked files match the configured writable mode."""
    if is_writable_mode(workspace_dir):
        return _apply_on(workspace_dir)
    return _apply_off(workspace_dir)


def writable_command(args: argparse.Namespace) -> int:
    """Execute the writable command."""
    workspace_dir = args.workspace_dir

    if args.writable_action == 'apply':
        return apply_writable_mode(workspace_dir)

    log.heading('Writable mode')
    log.success('on' if is_writable_mode(workspace_dir) else 'off')
    return 0
