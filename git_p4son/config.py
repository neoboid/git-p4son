"""
Configuration management for git-p4son.

Reads and writes per-repo config stored in .git-p4son/config.toml.
"""

import os
import tomllib

from . import CONFIG_DIR


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
    """Write config dict to the config file."""
    path = config_path(workspace_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w') as f:
        for section, values in config.items():
            f.write(f'[{section}]\n')
            for key, value in values.items():
                f.write(f'{key} = "{value}"\n')
            f.write('\n')


def get_depot_root(workspace_dir: str) -> str | None:
    """Get the depot root from config, or None if not configured."""
    config = load_config(workspace_dir)
    return config.get('depot', {}).get('root')
