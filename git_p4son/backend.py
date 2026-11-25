"""The backend: what a command asks of the tool holding the change."""

import contextlib
import os
from collections.abc import Callable, Iterator
from typing import ContextManager, Protocol

from . import CONFIG_DIR, jj
from .common import CommandError
from .changelist_store import aliases_by_change
from .config import ConfigError, get_setting
from .git_backend import GitBackend
from .jj_backend import JjBackend
from .lock import command_lock
from .log import log
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

    def implicit_base(self) -> str:
        """The base new and review take without -b, once check_on_sync_tip passed: the nearest branch or bookmark
        below the change.

        Raises CommandError, saying -b is required, where there is none."""

    def suggested_alias(self) -> str:
        """The alias the current branch or bookmark suggests; raise CommandError if there is none."""

    def change_id(self) -> str | None:
        """The jj change the command works on, or None for git."""

    def base_name(self, base: str) -> str:
        """The branch or bookmark base names, or the one at the commit it names; raise CommandError otherwise."""

    def commit_identities(self, commits: list[str]) -> list[str]:
        """What identifies each commit across rebases, in their order: the jj change ID, or git's author date."""

    def insert_empty_after(self, commit: str, message: str) -> str:
        """Insert an empty commit right after commit, with the commits after it, up to the tip, rebased onto it.

        Returns the new commit."""

    def existing_changes(self, change_ids: list[str]) -> set[str]:
        """The change_ids that still exist; all of them for git."""

    def nearest_change(self, change_ids: list[str]) -> str | None:
        """The one of change_ids nearest the change in its chain, above the nearest bookmark below it and not empty;
        None for none or several, and for git."""

    def subject(self) -> str:
        """The change's default message; raise CommandError if there is none."""

    def check_clean(self) -> None:
        """Raise CommandError, after listing them, if the change has uncommitted work."""

    def check_on_sync_tip(self) -> None:
        """Raise CommandError unless the sync branch exists and the change contains its tip, with no merge above it."""

    def check_base(self, base: str, name: str, recorded: bool = False) -> None:
        """Raise CommandError, calling it name, unless the resolved base is below the change and contains the sync
        branch's tip, once check_on_sync_tip passed; recorded if the alias records the base."""

    def base_candidates(self) -> list[tuple[str, str]]:
        """Completion candidates for -b, as (name, description)."""

    def checkouts(self) -> ContextManager[Callable[[str], None]]:
        """Yield a function that puts a commit's files in the work tree, returning to the start on exit."""

    def prepare_sync(self, branch: str | None) -> str:
        """Choose the sync branch, branch if given; returns the revision holding it. Raise CommandError if none fits."""

    def start_sync(self) -> None:
        """Put the sync branch's files in the work tree, ready for the pre-sync hooks and sync's first commit."""

    def resume_sync(self, sync_branch: str, tip: str, start: dict[str, str | bool]) -> str:
        """In place of prepare_sync for an interrupted sync: check sync_branch is where it left it, at tip, and take
        the start from start, the sync progress's [start]; returns the revision holding the sync branch.

        Raises CommandError, naming the way out, where it isn't."""

    def begin_resume(self) -> None:
        """Ready the work tree for a resume's passes, once its dry run and pre-sync hooks are past, which leave git
        and jj as they were."""

    def returns_to(self) -> str | None:
        """Where the sync returns to, once prepare_sync or resume_sync took its start; None for the sync branch."""

    def files_changed_since(self, commit: str) -> set[str]:
        """The files on disk that differ from commit, and the untracked ones not ignored, as slash paths."""

    def progress_start(self) -> tuple[str, dict[str, str | bool]]:
        """The sync branch prepare_sync chose, and where the sync started, as the sync progress's [start] holds it:
        the backend's keys, then on-sync-branch."""

    def commit_sync(self, subject: str) -> int:
        """Commit everything in the workspace as a sync commit on the sync branch; returns how many files it staged."""

    def abandon_sync(self) -> None:
        """Return to where start_sync started, when sync stops before syncing anything."""

    def fail_sync(self) -> None:
        """Say where a sync that failed partway leaves the work tree: on the sync branch, as Perforce left it."""

    def finish_sync(self, state: str, rebase: bool) -> None:
        """Return to where start_sync started, the sync branch now at state, unless changes are left; rebase is jj's."""

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
    """Open the backend for one command, holding the workspace's lock: jj's working-copy lock and operation in a jj
    workspace, else git-p4son's own.

    snapshot has jj snapshot the working copy first and reconcile after."""
    name = selected_backend(workspace_dir, choice)
    with _own_lock(workspace_dir), jj.operation(workspace_dir, lock_only=name == 'git',
                                                snapshot=snapshot) as workspace:
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
    """The alias the branch keyword names: the one recording the jj change, or the nearest change below it in its chain,
    unless new_alias, else the suggested one."""
    change = backend.change_id()
    if change is not None and not new_alias:
        by_change = aliases_by_change(workspace_dir)
        found = by_change.get(change)
        if found is None:
            nearest = backend.nearest_change(sorted(by_change))
            found = None if nearest is None else by_change.get(nearest)
        if found is not None:
            return found
    return backend.suggested_alias()


def alias_for_branch_or_none(backend: Backend, workspace_dir: str, new_alias: bool) -> str | None:
    """alias_for_branch, or None when the backend can't tell."""
    try:
        return alias_for_branch(backend, workspace_dir, new_alias)
    except (CommandError, OSError):
        return None


def resolve_branch_keyword(backend: Backend, workspace_dir: str, new_alias: bool) -> str:
    """alias_for_branch under a heading of its own, which shows what the branch keyword resolved to."""
    words = backend.words
    log.heading(f'Resolving alias from current {words.tool} {words.branch}')
    resolved = alias_for_branch(backend, workspace_dir, new_alias)
    log.success(f'branch -> {resolved}')
    return resolved


def _own_lock(workspace_dir: str) -> ContextManager[None]:
    """git-p4son's own lock on a workspace that is not a jj one, which has jj's."""
    return contextlib.nullcontext() if jj.is_workspace(workspace_dir) else command_lock(workspace_dir)


@contextlib.contextmanager
def workspace_lock(workspace_dir: str) -> Iterator[None]:
    """Hold the workspace's lock for init: jj's in a jj workspace, so jj does not snapshot halfway through, else
    git-p4son's own once the workspace has its config directory."""
    # Before that no command can be running in it, and an init that stops early leaves no directory behind.
    set_up = os.path.isdir(os.path.join(workspace_dir, CONFIG_DIR))
    with _own_lock(workspace_dir) if set_up else contextlib.nullcontext(), jj.operation(workspace_dir, lock_only=True):
        yield
