"""A minimal TOML writer for git-p4son's own files; reading uses tomllib."""

import re
from typing import Any

_BARE_KEY_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def format_toml(data: dict[str, Any]) -> str:
    """Format data as TOML: top-level scalars first, then a [table] per dict."""
    return ''.join(_format_scalars(data)) + ''.join(
        _format_table(_format_key(key), value)
        for key, value in data.items() if isinstance(value, dict))


def _format_string(value: str) -> str:
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _format_key(key: str) -> str:
    """Format a TOML key, quoting it when it is not a bare key."""
    if _BARE_KEY_RE.match(key):
        return key
    return _format_string(key)


def _format_value(value: object) -> str:
    """Format a TOML value: strings, booleans, numbers, and lists."""
    if isinstance(value, str):
        return _format_string(value)
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, list):
        return '[' + ', '.join(_format_value(item) for item in value) + ']'
    raise ValueError(
        f'Unsupported TOML value type: {type(value).__name__}')


def _format_scalars(table: dict[str, Any]) -> list[str]:
    return [f'{_format_key(key)} = {_format_value(value)}\n'
            for key, value in table.items() if not isinstance(value, dict)]


def _format_table(name: str, table: dict[str, Any]) -> str:
    """Format a [name] table; nested dicts become [name.sub] tables."""
    text = f'[{name}]\n' + ''.join(_format_scalars(table)) + '\n'
    for key, subtable in table.items():
        if isinstance(subtable, dict):
            text += _format_table(f'{name}.{_format_key(key)}', subtable)
    return text
