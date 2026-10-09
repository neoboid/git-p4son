"""Changelist aliases, stored in .git-p4son/changelists/<name>."""

import os
import re

from . import CONFIG_DIR
from .config import ensure_config_dir
from .log import log


RESERVED_KEYWORDS = frozenset({'last-synced', 'head', 'branch'})

# ASCII letters, digits, hyphen, underscore and dot, not starting or ending with a dot.
_ALIAS_NAME_RE = re.compile(r'^[A-Za-z0-9_-]([A-Za-z0-9._-]*[A-Za-z0-9_-])?$')

# Windows device names: opening one hits the device instead of creating a file.
_WINDOWS_RESERVED_NAMES = frozenset(
    {'con', 'prn', 'aux', 'nul'}
    | {f'com{i}' for i in range(1, 10)}
    | {f'lpt{i}' for i in range(1, 10)})


def validate_alias_name(name: str) -> str | None:
    """Return an error message if alias name is invalid, else None."""
    if not name:
        return 'Alias name cannot be empty'
    if name in RESERVED_KEYWORDS:
        return f'Alias name "{name}" is a reserved keyword'
    if not _ALIAS_NAME_RE.match(name):
        return (
            f'Invalid alias name "{name}": must contain only letters, digits, '
            'hyphens, underscores, and dots, and must not start or end with a dot')
    if name.isdigit():
        # Digit strings are taken as changelist numbers, so such an alias could never be used.
        return (
            f'Invalid alias name "{name}": an all-digit name would be '
            'indistinguishable from a changelist number')
    if name.split('.', 1)[0].lower() in _WINDOWS_RESERVED_NAMES:
        return (
            f'Invalid alias name "{name}": reserved device name on Windows')
    return None


def _changelists_dir(workspace_dir: str) -> str:
    """Return the path to the changelists alias directory."""
    return os.path.join(workspace_dir, CONFIG_DIR, 'changelists')


def _alias_path(name: str, workspace_dir: str) -> str | None:
    """Validate name and return its store path, or None if invalid (e.g. one escaping the store)."""
    error = validate_alias_name(name)
    if error:
        log.error(error)
        return None
    return os.path.join(_changelists_dir(workspace_dir), name)


def alias_exists(name: str, workspace_dir: str) -> bool:
    """Check whether a changelist alias exists."""
    alias_path = os.path.join(_changelists_dir(workspace_dir), name)
    return os.path.exists(alias_path)


def save_changelist_alias(name: str, changelist: str, workspace_dir: str, force: bool = False) -> bool:
    """Save a changelist number under a named alias."""
    alias_path = _alias_path(name, workspace_dir)
    if alias_path is None:
        return False

    changelists_dir = _changelists_dir(workspace_dir)

    if os.path.exists(alias_path) and not force:
        log.error(
            f'Alias "{name}" already exists (use -f/--force to overwrite)')
        return False

    ensure_config_dir(workspace_dir)
    if not os.path.isdir(changelists_dir):
        log.info(f'Creating {changelists_dir}')
        os.makedirs(changelists_dir, exist_ok=True)

    with open(alias_path, 'w') as f:
        f.write(changelist + '\n')

    return True


def load_changelist_alias(name: str, workspace_dir: str) -> str | None:
    """Load a changelist number from a named alias, or None if not found."""
    alias_path = _alias_path(name, workspace_dir)
    if alias_path is None:
        return None

    if not os.path.exists(alias_path):
        log.error(f'No changelist alias found: {name}')
        return None

    with open(alias_path, 'r') as f:
        content = f.read().strip()

    if not content:
        log.error(f'Changelist alias "{name}" is empty')
        return None

    return content


def list_changelist_aliases(workspace_dir: str) -> list[tuple[str, str]]:
    """Return all changelist aliases as sorted (name, changelist) tuples."""
    changelists_dir = _changelists_dir(workspace_dir)

    if not os.path.isdir(changelists_dir):
        return []

    aliases = []
    for name in os.listdir(changelists_dir):
        alias_path = os.path.join(changelists_dir, name)
        if os.path.isfile(alias_path):
            with open(alias_path, 'r') as f:
                content = f.read().strip()
            if content:
                aliases.append((name, content))

    return sorted(aliases, key=lambda x: x[0])


def delete_changelist_alias(name: str, workspace_dir: str) -> bool:
    """Delete a changelist alias file."""
    alias_path = _alias_path(name, workspace_dir)
    if alias_path is None:
        return False

    if not os.path.exists(alias_path):
        log.error(f'No changelist alias found: {name}')
        return False

    os.remove(alias_path)
    return True
