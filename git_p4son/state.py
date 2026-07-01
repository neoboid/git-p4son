"""
Local, per-user state for git-p4son.

Stored in .git-p4son/state.toml, which is kept out of version control (unlike
config.toml, which carries shared repo setup). State here reflects a single
user's workspace and preferences, e.g. dismissed warnings.
"""

import os

from . import CONFIG_DIR
from .config import load_toml, write_toml


def state_path(workspace_dir: str) -> str:
    """Return the path to the local state file."""
    return os.path.join(workspace_dir, CONFIG_DIR, 'state.toml')


def _load(workspace_dir: str) -> dict:
    return load_toml(state_path(workspace_dir))


def _save(workspace_dir: str, state: dict) -> None:
    write_toml(state_path(workspace_dir), state)
    _ensure_gitignored(workspace_dir)


def _ensure_gitignored(workspace_dir: str) -> None:
    """Make sure state.toml is ignored via a .gitignore inside CONFIG_DIR.

    A self-contained ignore file keeps the local state out of version control
    without touching the user's top-level .gitignore."""
    gitignore = os.path.join(workspace_dir, CONFIG_DIR, '.gitignore')
    entry = 'state.toml'
    existing = []
    if os.path.exists(gitignore):
        with open(gitignore, encoding='utf-8') as f:
            existing = [line.strip() for line in f]
        if entry in existing:
            return
    with open(gitignore, 'a', encoding='utf-8') as f:
        f.write(f'{entry}\n')


def is_clobber_warning_dismissed(workspace_dir: str) -> bool:
    """Return whether the user permanently dismissed the clobber warning."""
    return bool(_load(workspace_dir).get('clobber', {})
                .get('dismiss_warning', False))


def dismiss_clobber_warning(workspace_dir: str) -> None:
    """Persist that the clobber warning should never be shown again."""
    state = _load(workspace_dir)
    state.setdefault('clobber', {})['dismiss_warning'] = True
    _save(workspace_dir, state)
