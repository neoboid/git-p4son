"""Per-repo configuration stored in .git-p4son/config.toml."""

import os
import tomllib

from . import CONFIG_DIR
from .common import CommandError
from .toml_writer import format_toml


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


def load_config(workspace_dir: str) -> dict:
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


def _has_type(value, expected) -> bool:
    if expected == list[str]:
        return isinstance(value, list) and all(isinstance(item, str) for item in value)
    return isinstance(value, expected)


def get_setting(workspace_dir: str, section: str, key: str, expected, default):
    """Return config[section][key], or default when unset; expected is a type or list[str].

    Raises ConfigError when the section or the value has the wrong type.
    """
    table = load_config(workspace_dir).get(section, {})
    if not isinstance(table, dict):
        raise ConfigError(f'{section} in config.toml is not a table')
    if key not in table:
        return default
    value = table[key]
    if not _has_type(value, expected):
        raise ConfigError(f'{section}.{key} in config.toml must be '
                          f'{_TYPE_DESCRIPTIONS[expected]}')
    return value


def save_config(workspace_dir: str, config: dict) -> None:
    """Merge config into the config file, preserving sections and keys not named in config."""
    merged = load_config(workspace_dir)
    for section, values in config.items():
        merged.setdefault(section, {}).update(values)
    ensure_config_dir(workspace_dir)
    # tomllib reads strict UTF-8, so write UTF-8 explicitly.
    with open(config_path(workspace_dir), 'w', encoding='utf-8') as f:
        f.write(format_toml(merged))
