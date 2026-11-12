"""Per-repo configuration stored in .git-p4son/config.toml."""

import os
import re
import tomllib
from typing import Any, TypeVar

from . import CONFIG_DIR
from .common import CommandError
from .toml_writer import format_toml, format_value


def ensure_config_dir(workspace_dir: str) -> None:
    """Create the config dir with a .gitignore that ignores everything in it."""
    config_dir = os.path.join(workspace_dir, CONFIG_DIR)
    os.makedirs(config_dir, exist_ok=True)
    gitignore = os.path.join(config_dir, '.gitignore')
    content = '*\n'
    try:
        with open(gitignore, encoding='utf-8') as f:
            if f.read() == content:
                return
    except FileNotFoundError:
        pass
    with open(gitignore, 'w', encoding='utf-8') as f:
        f.write(content)


def config_path(workspace_dir: str) -> str:
    """Return the path to the config file."""
    return os.path.join(workspace_dir, CONFIG_DIR, 'config.toml')


class ConfigError(CommandError):
    """config.toml is not valid TOML, or a value in it has the wrong type."""


def load_config(workspace_dir: str) -> dict[str, Any]:
    """Read and parse the config file. Returns empty dict if missing; raise ConfigError if invalid."""
    path = config_path(workspace_dir)
    if not os.path.exists(path):
        return {}
    with open(path, 'rb') as f:
        try:
            return tomllib.load(f)
        except tomllib.TOMLDecodeError as error:
            raise ConfigError(f'{path} is not valid TOML: {error}') from error


_TYPE_DESCRIPTIONS = {
    str: 'a string',
    bool: 'true or false',
    dict: 'a table',
    list[str]: 'a list of strings',
}


def _has_type(value: object, expected: type[Any]) -> bool:
    if expected == list[str]:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return isinstance(value, expected)


_Value = TypeVar('_Value')
_Default = TypeVar('_Default')


def get_setting(workspace_dir: str, section: str, key: str, expected: type[_Value],
                default: _Default) -> _Value | _Default:
    """Return config[section][key], or default when unset; expected is a type or list[str].

    Raises ConfigError when the section or the value has the wrong type.
    """
    table = load_config(workspace_dir).get(section, {})
    if not isinstance(table, dict):
        raise ConfigError(f'{section} in config.toml is not a table')
    if key not in table:
        return default
    value: _Value = table[key]
    if not _has_type(value, expected):
        raise ConfigError(f'{section}.{key} in config.toml must be '
                          f'{_TYPE_DESCRIPTIONS[expected]}')
    return value


def save_config(workspace_dir: str, config: dict[str, dict[str, Any]]) -> None:
    """Merge config into the config file, preserving sections and keys not named in config."""
    merged = load_config(workspace_dir)
    for section, values in config.items():
        merged.setdefault(section, {}).update(values)
    ensure_config_dir(workspace_dir)
    # tomllib reads strict UTF-8, so write UTF-8 explicitly.
    with open(config_path(workspace_dir), 'w', encoding='utf-8') as f:
        f.write(format_toml(merged))


def _load(text: str) -> dict[str, Any] | None:
    """text parsed as TOML, or None if it isn't valid TOML."""
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return None


def set_setting_in_place(workspace_dir: str, section: str, key: str, value: str | bool | list[str]) -> None:
    """Set a setting in config.toml, replacing only its value and leaving the rest as written.

    Raises ConfigError, leaving the file alone, when the edited file doesn't read back with the value."""
    path = config_path(workspace_dir)
    try:
        with open(path, encoding='utf-8', newline='') as f:
            text = f.read()
    except FileNotFoundError:
        text = ''
    lines = text.splitlines(keepends=True)
    header = re.compile(rf'^\s*\[\s*{re.escape(section)}\s*\]\s*(#.*)?$')
    start = next((i for i, text_line in enumerate(
        lines) if header.match(text_line)), None)
    if start is None:
        # Ended as the file's last line is, so a CRLF file stays CRLF.
        newline = '\r\n' if lines and lines[-1].endswith('\r\n') else '\n'
        if lines:
            if not lines[-1].endswith('\n'):
                lines[-1] += newline
            lines.append(newline)
        lines.append(f'[{section}]{newline}')
        start = len(lines) - 1
    elif not lines[start].endswith('\n'):
        lines[start] += '\n'
    # Ended as the header is, so a CRLF file stays CRLF.
    line = f'{key} = {format_value(value)}' + \
        ('\r\n' if lines[start].endswith('\r\n') else '\n')
    end = next((i for i in range(start + 1, len(lines))
               if lines[i].lstrip().startswith('[')), len(lines))
    assignment = re.compile(rf'^\s*{re.escape(key)}\s*=')
    found = next((i for i in range(start + 1, end)
                 if assignment.match(lines[i])), None)
    if found is None:
        lines.insert(start + 1, line)
    else:
        # The value runs to the first line that completes it as TOML, as a value may span several.
        last = next((i for i in range(found, end) if _load(
            ''.join(lines[found:i + 1])) is not None), found)
        lines[found:last + 1] = [line]
    text = ''.join(lines)
    written = (_load(text) or {}).get(section, {}).get(key)
    if written != value:
        raise ConfigError(f'{path} holds {section}.{key} in a form git-p4son can\'t edit. '
                          f'Set {key} in [{section}] by hand.')
    ensure_config_dir(workspace_dir)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
