"""
Writable mode for git-p4son.

Writable mode keeps git-tracked files writable so they can be edited without a
manual p4 edit: git-p4son opens them in Perforce itself when creating or
updating changelists. Git-ignored files are left to Perforce as usual.
"""

from .config import load_config, save_config


def is_writable_mode(workspace_dir: str) -> bool:
    """Whether writable mode is on: git-tracked files are kept writable."""
    config = load_config(workspace_dir)
    return config.get('core', {}).get('writable') is True


def set_writable_mode(workspace_dir: str, enabled: bool) -> None:
    """Turn writable mode on or off in the config."""
    save_config(workspace_dir, {'core': {'writable': enabled}})
