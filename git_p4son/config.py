"""Per-repo configuration stored in .git-p4son/config.toml."""

import os
import tomllib
from typing import Any, TypeVar

from . import CONFIG_DIR
from .common import CommandError
from .toml_writer import format_key, format_toml, format_value


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


def _newline(line: str) -> str:
    return '\r\n' if line.endswith('\r\n') else '\n'


def _statements(lines: list[str]) -> list[tuple[int, int]]:
    """The (first, last) line of each table header and key/value in lines, blank and comment lines left out."""
    statements = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith('#'):
            # A value runs to the first line that completes it as TOML, as it may span several.
            last = i if stripped.startswith('[') else next(
                (j for j in range(i, len(lines)) if _load(''.join(lines[i:j + 1])) is not None), i)
            statements.append((i, last))
            i = last
        i += 1
    return statements


def _comments(span: list[str]) -> list[tuple[int, int]]:
    """Where each comment in a statement's lines starts, as (line, column), in order."""
    value = _load(''.join(span))
    if value is None:
        return []
    comments = []
    for n, line in enumerate(span):
        # A # starts a comment when cutting the line there leaves the value as it was; in a string it doesn't.
        for column in (i for i, char in enumerate(line) if char == '#'):
            cut = [*span[:n], line[:column] + _newline(line), *span[n + 1:]]
            if _load(''.join(cut)) == value:
                comments.append((n, column))
                break
    return comments


def _path(data: object) -> tuple[tuple[str, ...], object]:
    """The keys down through tables of one key each, and what they lead to."""
    path: list[str] = []
    while isinstance(data, dict) and len(data) == 1:
        ((name, data),) = data.items()
        path.append(name)
    return tuple(path), data


def _header(line: str) -> tuple[str, ...] | None:
    """The table a [table] header line names, or None for an array of tables."""
    path, rest = _path(_load(line))
    return path if rest == {} else None


def _key(line: str) -> tuple[tuple[str, ...], int] | None:
    """The key a key/value line sets, as TOML reads it, and where its = is; None if there is none."""
    # The key ends at the first = that leaves a key TOML reads, as a quoted key may hold an =.
    for end in (i for i, char in enumerate(line) if char == '='):
        if (data := _load(f'{line[:end]}= 0')) is not None:
            return _path(data)[0], end
    return None


def _set_in_lines(lines: list[str], section: str, key: str, value: str | bool | list[str]) -> None:
    """Set section.key to value in lines, as a key of its table, a dotted key or an inline table, as it is written."""
    statements = _statements(lines)
    headers = [(first, _header(lines[first]))
               for first, _ in statements if lines[first].lstrip().startswith('[')]
    first_header = headers[0][0] if headers else len(lines)
    keys = [(first, last, *found) for first, last in statements
            if not lines[first].lstrip().startswith('[') and (found := _key(lines[first]))]

    def replace(first: int, last: int, end: int, new_value: object) -> None:
        newline = _newline(lines[first])
        indent = lines[first][:len(lines[first]) - len(lines[first].lstrip())]
        comments = _comments(lines[first:last + 1])
        # The last line's comment stays after the value; the ones inside it go above the key.
        trailing = comments.pop(
        ) if comments and comments[-1][0] == last - first else None
        line = f'{lines[first][:end].rstrip()} = {format_value(new_value)}'
        if trailing is not None:
            before = lines[last][:trailing[1]]
            line += (before[len(before.rstrip()):] or ' ') + \
                lines[last][trailing[1]:].rstrip('\r\n')
        lines[first:last + 1] = [f'{indent}{lines[first + n][column:].rstrip()}{newline}' for n, column in comments] \
            + [f'{line}{newline}']

    top = [entry for entry in keys if entry[0] < first_header]
    for first, last, path, end in top:
        if path == (section, key):
            return replace(first, last, end, value)
        table = (_load(''.join(lines[first:last + 1])) or {}).get(section)
        if path == (section,) and isinstance(table, dict):
            return replace(first, last, end, table | {key: value})
    header_line = next(
        (first for first, path in headers if path == (section,)), None)
    if header_line is not None:
        if not lines[header_line].endswith('\n'):
            lines[header_line] += '\n'
        table_end = next(
            (first for first, _ in headers if first > header_line), len(lines))
        for first, last, path, end in keys:
            if header_line < first < table_end and path == (key,):
                return replace(first, last, end, value)
        lines.insert(
            header_line + 1, f'{format_key(key)} = {format_value(value)}{_newline(lines[header_line])}')
        return
    siblings = [last for first, last, path, _ in top if path[:1] == (section,)]
    if siblings:
        lines.insert(siblings[-1] + 1, f'{format_key(section)}.{format_key(key)} = {format_value(value)}'
                     f'{_newline(lines[siblings[-1]])}')
        return
    # Ended as the file's last line is, so a CRLF file stays CRLF.
    newline = _newline(lines[-1]) if lines else '\n'
    if lines:
        if not lines[-1].endswith('\n'):
            lines[-1] += newline
        lines.append(newline)
    lines += [f'[{format_key(section)}]{newline}',
              f'{format_key(key)} = {format_value(value)}{newline}']


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
    _set_in_lines(lines, section, key, value)
    text = ''.join(lines)
    written = (_load(text) or {}).get(section, {}).get(key)
    if written != value:
        raise ConfigError(f'{path} holds {section}.{key} in a form git-p4son can\'t edit. '
                          f'Set {key} in [{section}] by hand.')
    ensure_config_dir(workspace_dir)
    with open(path, 'w', encoding='utf-8', newline='') as f:
        f.write(text)
