"""The backend: what a command asks of the tool holding the change."""

import contextlib
from collections.abc import Callable, Iterator
from typing import ContextManager, Protocol

from . import jj
from .common import CommandError
from .changelist_store import alias_for_change
from .config import ConfigError, get_setting
from .git_backend import GitBackend
from .jj_backend import JjBackend
from .words import Words

BACKENDS = ('git', 'jj')


class Backend(Protocol):
    """What varies between the tools a change can live in."""

    words: Words

    @property
    def revision(self) -> str:
        """The commit to read the change from."""

    def resolve_base(self, base: str | None) -> str:
        """The base to find the change's commits since, None for the backend's default."""

    def suggested_alias(self) -> str:
        """The alias the current branch or bookmark suggests; raise CommandError if there is none."""

    def change_id(self) -> str | None:
        """The jj change the command works on, or None for git."""

    def existing_changes(self, change_ids: list[str]) -> set[str]:
        """The change_ids that still exist; all of them for git."""

    def subject(self) -> str:
        """The change's default message; raise CommandError if there is none."""

    def check_clean(self) -> None:
        """Raise CommandError, after listing them, if the change has uncommitted work."""

    def base_candidates(self) -> list[tuple[str, str]]:
        """Completion candidates for -b, as (name, description)."""

    def checkouts(self) -> ContextManager[Callable[[str], None]]:
        """Yield a function that puts a commit's files in the work tree, returning to the start on exit."""

    def prepare_sync(self, branch: str | None) -> str:
        """Choose the sync branch, branch if given; returns the revision holding it. Raise CommandError if none fits."""

    def check_sync_clean(self) -> None:
        """Raise CommandError, after listing them, if git sees changes in the files sync is about to commit over."""

    def start_sync(self) -> None:
        """Put the sync branch's files in the work tree, ready for the pre-sync hooks and sync's first commit."""

    def commit_sync(self, subject: str) -> int:
        """Commit everything in the workspace as a sync commit on the sync branch; returns how many files it staged."""

    def abandon_sync(self) -> None:
        """Return to where start_sync started, when sync stops before syncing anything."""

    def fail_sync(self) -> None:
        """Say where a sync that failed partway leaves the work tree: on the sync branch, as Perforce left it."""

    def finish_sync(self, changelist: int | None, rebase: bool) -> None:
        """Return to where start_sync started, now at changelist, unless changes are left; rebase applies to jj."""

    def after_lock(self) -> None:
        """Finish what the command left for after jj's working-copy lock is released, as jj's rebase."""


def selected_backend(workspace_dir: str, choice: str | None) -> str:
    """The backend to use: choice, else core.backend, where auto means jj in a jj workspace.

    Raises CommandError for jj outside a jj workspace, and ConfigError for an unknown setting."""
    setting = choice or get_setting(
        workspace_dir, 'core', 'backend', str, 'auto')
    if setting not in ('auto', *BACKENDS):
        raise ConfigError(
            f'core.backend in config.toml must be one of {", ".join(("auto", *BACKENDS))}, not "{setting}"')
    in_jj = jj.is_workspace(workspace_dir)
    if setting == 'auto':
        return 'jj' if in_jj else 'git'
    if setting == 'jj' and not in_jj:
        raise CommandError(
            'The jj backend needs a colocated jj workspace, and this one has no .jj directory')
    return setting


@contextlib.contextmanager
def open_backend(workspace_dir: str, choice: str | None, *, snapshot: bool = False) -> Iterator[Backend]:
    """Open the backend for one command, holding jj's working-copy lock and operation in a jj workspace.

    snapshot has jj snapshot the working copy first and reconcile after."""
    name = selected_backend(workspace_dir, choice)
    with jj.operation(workspace_dir, lock_only=name == 'git', snapshot=snapshot) as workspace:
        backend: Backend = GitBackend(
            workspace_dir) if workspace is None else JjBackend(workspace)
        yield backend
    backend.after_lock()


def completion_backend(workspace_dir: str, choice: str | None) -> Backend:
    """The backend for completing a command line, read without jj's lock; raise CommandError if jj fails."""
    if selected_backend(workspace_dir, choice) == 'jj':
        return JjBackend(jj.Workspace.load(workspace_dir, '@'))
    return GitBackend(workspace_dir)


def alias_for_branch(backend: Backend, workspace_dir: str, new_alias: bool) -> str:
    """The alias the branch keyword names: the one recording the jj change, unless new_alias, else the suggested one."""
    change = backend.change_id()
    if change is not None and not new_alias:
        found = alias_for_change(change, workspace_dir)
        if found is not None:
            return found
    return backend.suggested_alias()


@contextlib.contextmanager
def workspace_lock(workspace_dir: str) -> Iterator[None]:
    """Hold jj's working-copy lock, in a jj workspace, so jj does not snapshot halfway through."""
    with jj.operation(workspace_dir, lock_only=True):
        yield
