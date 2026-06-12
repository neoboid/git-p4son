"""
Configuration management for git-p4son.

Reads and writes per-repo config stored in .git-p4son/config.toml.
"""

import os
import re
import tomllib

from . import CONFIG_DIR

_BARE_KEY_RE = re.compile(r'^[A-Za-z0-9_-]+$')


def config_path(workspace_dir: str) -> str:
    """Return the path to the config file."""
    return os.path.join(workspace_dir, CONFIG_DIR, 'config.toml')


def load_config(workspace_dir: str) -> dict:
    """Read and parse the config file. Returns empty dict if missing."""
    path = config_path(workspace_dir)
    if not os.path.exists(path):
        return {}
    with open(path, 'rb') as f:
        return tomllib.load(f)


def save_config(workspace_dir: str, config: dict) -> None:
    """Merge config into the config file, section by section.

    Sections not named in config are preserved (e.g. a configured [hooks]
    section survives re-running init); keys within a named section are
    updated rather than replacing the section wholesale."""
    merged = load_config(workspace_dir)
    for section, values in config.items():
        merged.setdefault(section, {}).update(values)

    path = config_path(workspace_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # tomllib reads strict UTF-8, so write UTF-8 explicitly.
    with open(path, 'w', encoding='utf-8') as f:
        for section, values in merged.items():
            _write_table(f, _format_key(section), values)


def _format_string(value: str) -> str:
    escaped = value.replace('\\', '\\\\').replace('"', '\\"')
    return f'"{escaped}"'


def _format_key(key: str) -> str:
    """Format a TOML key, quoting it when it is not a bare key."""
    if _BARE_KEY_RE.match(key):
        return key
    return _format_string(key)


def _format_value(value) -> str:
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
        f'Unsupported config value type: {type(value).__name__}')


def _write_table(f, name: str, table: dict) -> None:
    """Write a [name] table; nested dicts become [name.sub] tables."""
    scalars = {k: v for k, v in table.items() if not isinstance(v, dict)}
    subtables = {k: v for k, v in table.items() if isinstance(v, dict)}
    f.write(f'[{name}]\n')
    for key, value in scalars.items():
        f.write(f'{_format_key(key)} = {_format_value(value)}\n')
    f.write('\n')
    for key, subtable in subtables.items():
        _write_table(f, f'{name}.{_format_key(key)}', subtable)


def get_depot_root(workspace_dir: str) -> str | None:
    """Get the depot root from config, or None if not configured."""
    config = load_config(workspace_dir)
    return config.get('depot', {}).get('root')
