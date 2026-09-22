"""
Writable mode for git-p4son.

Writable mode keeps git-tracked files writable so they can be edited without a
manual p4 edit: git-p4son opens them in Perforce itself when creating or
updating changelists. Git-ignored files are left to Perforce as usual.
"""

import os
import stat

from .config import load_config, save_config


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
