"""Changelist aliases, stored in .git-p4son/aliases/<name>.toml, still read from the old .git-p4son/changelists/."""

import os
import re
import tomllib
from collections.abc import Callable
from typing import TypeVar

from . import CONFIG_DIR
from .common import CommandError
from .config import ensure_config_dir
from .log import log
from .toml_writer import format_toml

_Read = TypeVar('_Read')


RESERVED_KEYWORDS = frozenset({'last-synced', 'head', 'branch'})

# ASCII letters, digits, hyphen, underscore and dot, not starting or ending with a dot.
_ALIAS_NAME_RE = re.compile(r'^[A-Za-z0-9_-]([A-Za-z0-9._-]*[A-Za-z0-9_-])?$')

# jj change IDs are written with the letters k to z.
_CHANGE_ID_RE = re.compile(r'[k-z]+')

# Windows device names: opening one hits the device instead of creating a file.
_WINDOWS_RESERVED_NAMES = frozenset(
    {'con', 'prn', 'aux', 'nul'}
    | {f'com{i}' for i in range(1, 10)}
    | {f'lpt{i}' for i in range(1, 10)})


def branch_to_alias(branch_name: str) -> str:
    """Sanitize a branch name for use as an alias filename."""
    return branch_name.replace('/', '-')


def branch_alias(branch_name: str) -> str | None:
    """The alias a branch name maps to, or None if that is a reserved keyword."""
    alias = branch_to_alias(branch_name)
    return None if alias in RESERVED_KEYWORDS else alias


def alias_for(name: str) -> str:
    """The alias a branch or bookmark name suggests; raise CommandError if that is a reserved keyword."""
    alias = branch_alias(name)
    if alias is None:
        raise CommandError(
            f'Branch "{name}" resolves to reserved keyword "{branch_to_alias(name)}"')
    return alias


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


def _aliases_dir(workspace_dir: str) -> str:
    return os.path.join(workspace_dir, CONFIG_DIR, 'aliases')


def _old_aliases_dir(workspace_dir: str) -> str:
    return os.path.join(workspace_dir, CONFIG_DIR, 'changelists')


def _alias_paths(name: str, workspace_dir: str) -> tuple[str, str]:
    """Validate name and return its path in the store and in the old one; raise CommandError if invalid."""
    error = validate_alias_name(name)
    if error:
        raise CommandError(error)
    return (os.path.join(_aliases_dir(workspace_dir), f'{name}.toml'),
            os.path.join(_old_aliases_dir(workspace_dir), name))


def _no_alias(name: str) -> CommandError:
    return CommandError(f'No changelist alias found: {name}. '
                        'See "git p4son alias list".')


def _read_alias_file(path: str, name: str) -> tuple[str, str | None]:
    """The changelist and the jj change an alias file records; raise CommandError if it has no changelist."""
    with open(path, 'rb') as f:
        try:
            data = tomllib.load(f)
        except tomllib.TOMLDecodeError as error:
            raise CommandError(f'{path} is not valid TOML: {error}') from error
    changelist = data.get('changelist')
    if type(changelist) is not int:
        raise CommandError(
            f'Changelist alias "{name}" has no changelist number')
    change = data.get('change')
    valid = isinstance(change, str) and _CHANGE_ID_RE.fullmatch(change)
    return str(changelist), change if valid else None


def _read_old_alias(path: str, name: str) -> str:
    """The changelist number in an old store's alias file; raise CommandError if it is empty."""
    with open(path, 'r') as f:
        content = f.read().strip()
    if not content:
        raise CommandError(f'Changelist alias "{name}" is empty')
    return content


def _remove_old_alias(old_path: str, workspace_dir: str) -> None:
    """Remove an alias from the old store, and the old store once it is empty."""
    if os.path.exists(old_path):
        os.remove(old_path)
    try:
        os.rmdir(_old_aliases_dir(workspace_dir))
    except OSError:
        pass


def _write_alias(name: str, changelist: str, workspace_dir: str, change: str | None = None) -> None:
    path, old_path = _alias_paths(name, workspace_dir)
    if not changelist.isdigit():
        raise CommandError(f'Invalid changelist number: {changelist}')
    ensure_config_dir(workspace_dir)
    aliases_dir = _aliases_dir(workspace_dir)
    if not os.path.isdir(aliases_dir):
        log.info(f'Creating {aliases_dir}')
        os.makedirs(aliases_dir, exist_ok=True)
    data: dict[str, int | str] = {'changelist': int(changelist)}
    if change is not None:
        data['change'] = change
    with open(path, 'w', encoding='utf-8') as f:
        f.write(format_toml(data))
    _remove_old_alias(old_path, workspace_dir)


def _listed(directory: str, suffix: str, read: Callable[[str, str], _Read]) -> dict[str, _Read]:
    """The readable aliases in a store directory, by name; files that hold none are left out."""
    if not os.path.isdir(directory):
        return {}
    aliases = {}
    for entry in os.listdir(directory):
        path = os.path.join(directory, entry)
        name = entry.removesuffix(suffix)
        if (not entry.endswith(suffix) or not os.path.isfile(path)
                or validate_alias_name(name)):
            continue
        try:
            aliases[name] = read(path, name)
        except CommandError:
            continue
    return aliases


def _records(workspace_dir: str) -> dict[str, tuple[str, str | None]]:
    return _listed(_aliases_dir(workspace_dir), '.toml', _read_alias_file)


def _listed_new(workspace_dir: str) -> dict[str, str]:
    return {name: changelist for name, (changelist, _) in _records(workspace_dir).items()}


def _listed_old(workspace_dir: str) -> dict[str, str]:
    return _listed(_old_aliases_dir(workspace_dir), '', _read_old_alias)


def check_alias_usable(name: str, workspace_dir: str, force: bool) -> None:
    """Raise CommandError unless name is a valid alias that is free, or force is set."""
    path, old_path = _alias_paths(name, workspace_dir)
    if not force and (os.path.exists(path) or os.path.exists(old_path)):
        raise CommandError(
            f'Alias "{name}" already exists (use -f/--force to overwrite)')


def save_changelist_alias(name: str, changelist: str, workspace_dir: str, force: bool,
                          change: str | None = None) -> None:
    """Save a changelist under an alias, moving the record of change to it; raise CommandError if not usable."""
    check_alias_usable(name, workspace_dir, force)
    if change is not None:
        for other, (other_changelist, recorded) in _records(workspace_dir).items():
            if recorded == change and other != name:
                _write_alias(other, other_changelist, workspace_dir)
    _write_alias(name, changelist, workspace_dir, change)


def alias_changes(workspace_dir: str) -> dict[str, str]:
    """The jj change each alias records, by alias name, for the aliases that record one."""
    return {name: change for name, (_, change) in _records(workspace_dir).items() if change is not None}


def alias_for_change(change: str, workspace_dir: str) -> str | None:
    """The alias that records change, or None."""
    return next((name for name, recorded in sorted(alias_changes(workspace_dir).items())
                 if recorded == change), None)


def load_changelist_alias(name: str, workspace_dir: str) -> str:
    """Load a changelist number from a named alias; raise CommandError if there is none."""
    path, old_path = _alias_paths(name, workspace_dir)
    if os.path.exists(path):
        return _read_alias_file(path, name)[0]
    if os.path.exists(old_path):
        return _read_old_alias(old_path, name)
    raise _no_alias(name)


def list_changelist_aliases(workspace_dir: str) -> list[tuple[str, str]]:
    """Return all changelist aliases as sorted (name, changelist) tuples."""
    aliases = _listed_old(workspace_dir) | _listed_new(workspace_dir)
    return sorted(aliases.items())


def delete_changelist_alias(name: str, workspace_dir: str) -> None:
    """Delete a changelist alias from both stores; raise CommandError if there is none."""
    path, old_path = _alias_paths(name, workspace_dir)
    if not os.path.exists(path) and not os.path.exists(old_path):
        raise _no_alias(name)
    if os.path.exists(path):
        os.remove(path)
    _remove_old_alias(old_path, workspace_dir)


def migrate_changelist_aliases(workspace_dir: str) -> int:
    """Move the aliases in the old store to the new one, and return how many moved.

    An alias in both stores keeps the new one, as reads do.
    """
    current = _listed_new(workspace_dir)
    moved = 0
    for name, changelist in _listed_old(workspace_dir).items():
        if name in current:
            _remove_old_alias(_alias_paths(
                name, workspace_dir)[1], workspace_dir)
        else:
            _write_alias(name, changelist, workspace_dir)
            moved += 1
    return moved
