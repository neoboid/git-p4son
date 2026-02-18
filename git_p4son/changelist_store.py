"""
Changelist alias utilities for git-p4son.

Stores named aliases for changelist numbers in .git-p4son/changelists/<name>.
"""

import os
import sys


def _changelists_dir(workspace_dir: str) -> str:
    """Return the path to the changelists alias directory."""
    return os.path.join(workspace_dir, '.git-p4son', 'changelists')


def save_changelist_alias(name: str, changelist: str, workspace_dir: str, force: bool = False) -> bool:
    """
    Save a changelist number under a named alias.

    Creates .git-p4son/changelists/ directory if needed and writes the
    changelist number to .git-p4son/changelists/<name>.

    Args:
        name: The alias name
        changelist: The changelist number to store
        workspace_dir: The workspace root directory
        force: If True, overwrite an existing alias file

    Returns:
        True on success, False on failure
    """
    if '@' in name:
        print('Alias name cannot contain "@"', file=sys.stderr)
        return False

    changelists_dir = _changelists_dir(workspace_dir)
    alias_path = os.path.join(changelists_dir, name)

    if os.path.exists(alias_path) and not force:
        print(f'Alias "{name}" already exists (use -f/--force to overwrite)',
              file=sys.stderr)
        return False

    os.makedirs(changelists_dir, exist_ok=True)

    with open(alias_path, 'w') as f:
        f.write(changelist + '\n')

    return True


def load_changelist_alias(name: str, workspace_dir: str) -> str | None:
    """
    Load a changelist number from a named alias.

    Args:
        name: The alias name
        workspace_dir: The workspace root directory

    Returns:
        The changelist number string, or None if not found
    """
    alias_path = os.path.join(_changelists_dir(workspace_dir), name)

    if not os.path.exists(alias_path):
        print(f'No changelist alias found: {name}', file=sys.stderr)
        return None

    with open(alias_path, 'r') as f:
        content = f.read().strip()

    if not content:
        print(f'Changelist alias "{name}" is empty', file=sys.stderr)
        return None

    return content


def resolve_changelist(value: str, workspace_dir: str) -> str | None:
    """
    Resolve a changelist value that may be a number or a named alias.

    If the value is all digits, it is returned as-is.
    Otherwise it is looked up as an alias.

    Args:
        value: A changelist number or alias name
        workspace_dir: The workspace root directory

    Returns:
        The changelist number string, or None if alias lookup failed
    """
    if value.isdigit():
        return value
    return load_changelist_alias(value, workspace_dir)


def list_changelist_aliases(workspace_dir: str) -> list[tuple[str, str]]:
    """
    Return a list of all changelist aliases and their values.

    Args:
        workspace_dir: The workspace root directory

    Returns:
        Sorted list of (alias_name, changelist_number) tuples.
    """
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
    """
    Delete a changelist alias file.

    Args:
        name: The alias name to delete
        workspace_dir: The workspace root directory

    Returns:
        True on success, False if not found
    """
    alias_path = os.path.join(_changelists_dir(workspace_dir), name)

    if not os.path.exists(alias_path):
        print(f'No changelist alias found: {name}', file=sys.stderr)
        return False

    os.remove(alias_path)
    return True
