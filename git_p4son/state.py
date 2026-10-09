"""Local, per-user state stored in .git-p4son/state.toml."""

import os

from . import CONFIG_DIR
from .config import ensure_config_dir, load_toml, write_toml


def state_path(workspace_dir: str) -> str:
    """Return the path to the local state file."""
    return os.path.join(workspace_dir, CONFIG_DIR, 'state.toml')


def _load(workspace_dir: str) -> dict:
    return load_toml(state_path(workspace_dir))


def _save(workspace_dir: str, state: dict) -> None:
    ensure_config_dir(workspace_dir)
    write_toml(state_path(workspace_dir), state)


def is_clobber_warning_dismissed(workspace_dir: str) -> bool:
    """Return whether the user permanently dismissed the clobber warning."""
    return bool(_load(workspace_dir).get('clobber', {})
                .get('dismiss_warning', False))


def dismiss_clobber_warning(workspace_dir: str) -> None:
    """Persist that the clobber warning should never be shown again."""
    state = _load(workspace_dir)
    state.setdefault('clobber', {})['dismiss_warning'] = True
    _save(workspace_dir, state)
