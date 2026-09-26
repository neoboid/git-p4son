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
from .perforce import get_existing_p4_users, get_p4_user

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
    """The current Perforce user, or None when p4 cannot tell.

    OSError covers p4 not being installed at all."""
    try:
        return get_p4_user(workspace_dir)
    except (CommandError, OSError):
        return None


def resolve_split_users(users: list[str],
                        workspace_dir: str) -> list[str] | None:
    """Substitute the current Perforce user for $(user).

    Repeated names are collapsed, keeping the order they were given in.
    Returns None (with an error logged) when $(user) cannot be resolved."""
    current = None
    if USER_PLACEHOLDER in users:
        current = _current_user(workspace_dir)
        if not current:
            log.error(f'Cannot determine the current Perforce user for '
                      f'{USER_PLACEHOLDER} in the split users. Check the p4 '
                      'connection, or pass --no-split to sync without '
                      'the configured split users.')
            return None
    resolved: list[str] = []
    seen: set[str] = set()
    for user in users:
        name = current if user == USER_PLACEHOLDER else user
        if name.lower() not in seen:
            seen.add(name.lower())
            resolved.append(name)
    return resolved


def _list(workspace_dir: str) -> int:
    """Print the configured split users, one per line."""
    log.heading('Split users')
    users = get_split_users(workspace_dir)
    if not users:
        log.info('No split users configured. Add yourself with: '
                 'git p4son sync-split-users add --me')
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


def _requested(names: list[str], me: bool) -> list[str]:
    """The names given on the command line, --me as $(user) first.

    Repeated names are collapsed, keeping the order they were given in."""
    requested: list[str] = []
    seen: set[str] = set()
    for name in ([USER_PLACEHOLDER] if me else []) + names:
        if name.lower() not in seen:
            seen.add(name.lower())
            requested.append(name)
    return requested


def check_p4_users(names: list[str],
                   workspace_dir: str) -> dict[str, str] | None:
    """Check names against the server, mapping each to its server spelling.

    Returns None (with an error logged per unknown name) if any is not a
    Perforce user."""
    log.heading('Checking Perforce users')
    existing = {user.lower(): user
                for user in get_existing_p4_users(names, workspace_dir)}
    unknown = [name for name in names if name.lower() not in existing]
    if unknown:
        for name in unknown:
            log.error(f'No such Perforce user: {name}')
        return None
    canonical = {name: existing[name.lower()] for name in names}
    log.success(', '.join(canonical.values()))
    return canonical


def _add(workspace_dir: str, names: list[str], me: bool) -> int:
    """Add users to the split users, all or none of them."""
    requested = _requested(names, me)
    if not requested:
        log.error('Give one or more user names, or --me for yourself')
        return 1

    # A misspelled name would never match a changelist owner, so every name
    # is checked before anything is written. $(user) is resolved each time
    # it is used, so there is nothing to check for it here.
    real = [name for name in requested if name != USER_PLACEHOLDER]
    canonical: dict[str, str] = {}
    if real:
        checked = check_p4_users(real, workspace_dir)
        if checked is None:
            return 1
        canonical = checked

    log.heading('Adding split users')
    users = get_split_users(workspace_dir)
    present = {user.lower() for user in users}
    added = 0
    for name in requested:
        name = canonical.get(name, name)
        if name.lower() in present:
            log.info(f'{name} is already a split user')
            continue
        users.append(name)
        present.add(name.lower())
        log.success(name)
        added += 1
    if added:
        set_split_users(workspace_dir, users)
    return 0


def _delete(workspace_dir: str, names: list[str], me: bool) -> int:
    """Remove users from the split users, all or none of them."""
    requested = _requested(names, me)
    if not requested:
        log.error('Give one or more user names, or --me for yourself')
        return 1

    users = get_split_users(workspace_dir)
    present = {user.lower() for user in users}
    missing = [name for name in requested if name.lower() not in present]
    if missing:
        # Naming yourself when the list holds $(user) is an easy slip, since
        # list shows the placeholder next to your name.
        current = (_current_user(workspace_dir)
                   if USER_PLACEHOLDER in users else None)
        for name in missing:
            log.error(f'{name} is not a split user')
            if current and name.lower() == current.lower():
                log.info(f'{USER_PLACEHOLDER} stands for {current}, '
                         'remove it with --me')
        return 1

    log.heading('Removing split users')
    removed = {name.lower() for name in requested}
    for user in users:
        if user.lower() in removed:
            log.success(user)
    set_split_users(workspace_dir,
                    [user for user in users if user.lower() not in removed])
    return 0


def sync_split_users_command(args: argparse.Namespace) -> int:
    """Execute the sync-split-users command."""
    if args.split_users_action == 'add':
        return _add(args.workspace_dir, args.names, args.me)
    if args.split_users_action == 'delete':
        return _delete(args.workspace_dir, args.names, args.me)
    return _list(args.workspace_dir)
