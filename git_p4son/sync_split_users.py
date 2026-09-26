"""
Split users for git-p4son.

Split users are the Perforce users whose submitted changelists sync gives a
git commit each, holding nothing but that change. They are stored as a list
in the [sync] section of .git-p4son/config.toml.
"""

import argparse

from .common import CommandError
from .config import load_config, save_config
from .log import log
from .perforce import get_p4_user

# Placeholder allowed in the split users list, substituted with the current
# Perforce user each time the list is used, so the config survives a change
# of p4 user and can be shared between people.
USER_PLACEHOLDER = '$(user)'


def get_split_users(workspace_dir: str) -> list[str]:
    """The configured split users, in the order they were added.

    Anything other than a list of strings, e.g. from a hand-edited config,
    is ignored rather than guessed at."""
    config = load_config(workspace_dir)
    users = config.get('sync', {}).get('split-users', [])
    if not isinstance(users, list):
        return []
    return [user for user in users if isinstance(user, str)]


def set_split_users(workspace_dir: str, users: list[str]) -> None:
    """Replace the configured split users."""
    save_config(workspace_dir, {'sync': {'split-users': users}})


def _current_user(workspace_dir: str) -> str | None:
    """The current Perforce user, or None when p4 cannot tell."""
    try:
        return get_p4_user(workspace_dir)
    except CommandError:
        return None


def _list(workspace_dir: str) -> int:
    """Print the configured split users, one per line."""
    log.heading('Split users')
    users = get_split_users(workspace_dir)
    if not users:
        log.info('No split users configured. Add one with: '
                 'git p4son sync-split-users add NAME')
        return 0
    # Show who $(user) stands for, but a p4 hiccup should not stop the
    # list from printing: the placeholder is shown bare instead.
    current = (_current_user(workspace_dir)
               if USER_PLACEHOLDER in users else None)
    for user in users:
        if user == USER_PLACEHOLDER and current:
            log.info(f'{user} ({current})')
        else:
            log.info(user)
    return 0


def sync_split_users_command(args: argparse.Namespace) -> int:
    """Execute the sync-split-users command."""
    return _list(args.workspace_dir)
