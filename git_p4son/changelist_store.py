"""
Changelist alias utilities for git-p4son.

Stores named aliases for changelist numbers in .git-p4son/changelists/<name>.
"""

import os

from .log import log


RESERVED_KEYWORDS = frozenset({'last-synced', 'branch'})


def _changelists_dir(workspace_dir: str) -> str:
    """Return the path to the changelists alias directory."""
    return os.path.join(workspace_dir, '.git-p4son', 'changelists')


def alias_exists(name: str, workspace_dir: str) -> bool:
    """Check whether a changelist alias exists."""
    alias_path = os.path.join(_changelists_dir(workspace_dir), name)
    return os.path.exists(alias_path)


def save_changelist_alias(name: str, changelist: str, workspace_dir: str, force: bool = False) -> bool:
    """Save a changelist number under a named alias."""
    if name in RESERVED_KEYWORDS:
        log.error(f'Alias name "{name}" is a reserved keyword')
        return False

    changelists_dir = _changelists_dir(workspace_dir)
    alias_path = os.path.join(changelists_dir, name)

    if os.path.exists(alias_path) and not force:
        log.error(
            f'Alias "{name}" already exists (use -f/--force to overwrite)')
        return False

    if not os.path.isdir(changelists_dir):
        log.info(f'Creating {changelist_dir}')
        os.makedirs(changelists_dir, exist_ok=True)

    with open(alias_path, 'w') as f:
        f.write(changelist + '\n')

    return True


def load_changelist_alias(name: str, workspace_dir: str) -> str | None:
    """Load a changelist number from a named alias, or None if not found."""
    alias_path = os.path.join(_changelists_dir(workspace_dir), name)

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
    alias_path = os.path.join(_changelists_dir(workspace_dir), name)

    if not os.path.exists(alias_path):
        log.error(f'No changelist alias found: {name}')
        return False

    os.remove(alias_path)
    return True
