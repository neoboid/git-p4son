"""A minimal TOML writer for git-p4son's own files; reading uses tomllib."""

import datetime
import re
from typing import Any

_BARE_KEY_RE = re.compile(r'^[A-Za-z0-9_-]+$')
_ESCAPES = {'\\': '\\\\', '"': '\\"', '\b': '\\b',
            '\t': '\\t', '\n': '\\n', '\f': '\\f', '\r': '\\r'}


def format_toml(data: dict[str, Any]) -> str:
    """Format data as TOML: top-level scalars first, then a [table] per dict."""
    return ''.join(_format_scalars(data)) + ''.join(
        _format_table(format_key(key), value)
        for key, value in data.items() if isinstance(value, dict))


def format_string(value: str) -> str:
    """A TOML basic string holding value."""
    # A basic string may hold no control character but tab raw, so all of them are escaped.
    escaped = ''.join(_ESCAPES.get(char) or (f'\\u{ord(char):04X}' if ord(char) < 0x20 or char == '\x7f' else char)
                      for char in value)
    return f'"{escaped}"'


def format_key(key: str) -> str:
    """Format a TOML key, quoting it when it is not a bare key."""
    if _BARE_KEY_RE.match(key):
        return key
    return format_string(key)


def format_value(value: object) -> str:
    """Format a TOML value: strings, booleans, numbers, dates and times, lists, and dicts as inline tables."""
    if isinstance(value, str):
        return format_string(value)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, list):
        return '[' + ', '.join(format_value(item) for item in value) + ']'
    if isinstance(value, dict):
        if not value:
            return '{}'
        return '{ ' + ', '.join(f'{format_key(key)} = {format_value(item)}' for key, item in value.items()) + ' }'
    raise ValueError(
        f'Unsupported TOML value type: {type(value).__name__}')


def _format_scalars(table: dict[str, Any]) -> list[str]:
    return [f'{format_key(key)} = {format_value(value)}\n'
            for key, value in table.items() if not isinstance(value, dict)]


def _format_table(name: str, table: dict[str, Any]) -> str:
    """Format a [name] table; nested dicts become [name.sub] tables."""
    text = f'[{name}]\n' + ''.join(_format_scalars(table)) + '\n'
    for key, subtable in table.items():
        if isinstance(subtable, dict):
            text += _format_table(f'{name}.{format_key(key)}', subtable)
    return text
