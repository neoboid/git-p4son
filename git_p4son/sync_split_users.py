"""
Split users for git-p4son.

Split users are the Perforce users whose submitted changelists sync gives a
git commit each, holding nothing but that change. They are stored as a list
in the [sync] section of .git-p4son/config.toml.
"""

from .config import load_config, save_config

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
