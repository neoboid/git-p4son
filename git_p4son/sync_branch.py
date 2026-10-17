"""The sync branch: the branch (git) or bookmark (jj) that sync commits land on, [sync] branch, usually main."""

from collections.abc import Callable

from .common import CommandError
from .config import get_setting

DEFAULT_SYNC_BRANCH = 'main'


def get_sync_branch_setting(workspace_dir: str) -> str | None:
    """The [sync] branch setting, or None if unset; raise ConfigError if malformed."""
    return get_setting(workspace_dir, 'sync', 'branch', str, None)


def choose_sync_branch(workspace_dir: str, branch: str | None, exists: Callable[[str], bool], kind: str) -> str:
    """branch, else [sync] branch, else main if it exists; raise CommandError if none exists.

    kind is "branch" or "bookmark", for the messages."""
    name = branch or get_sync_branch_setting(workspace_dir)
    if name is None:
        if not exists(DEFAULT_SYNC_BRANCH):
            raise CommandError(
                f'No sync {kind}. Set [sync] branch in .git-p4son/config.toml, or run git p4son init.')
        return DEFAULT_SYNC_BRANCH
    if not exists(name):
        raise CommandError(f'Sync {kind} "{name}" does not exist')
    return name
