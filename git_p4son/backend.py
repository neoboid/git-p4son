"""The backend: what a command asks of the tool holding the change."""

import contextlib
from collections.abc import Callable, Iterator
from typing import ContextManager, Protocol

from . import jj
from .git_backend import GitBackend
from .jj_backend import JjBackend
from .words import Words


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

    def subject(self) -> str:
        """The change's default message; raise CommandError if there is none."""

    def check_clean(self) -> None:
        """Raise CommandError, after listing them, if the change has uncommitted work."""

    def base_candidates(self) -> list[tuple[str, str]]:
        """Completion candidates for -b, as (name, description)."""

    def checkouts(self) -> ContextManager[Callable[[str], None]]:
        """Yield a function that puts a commit's files in the work tree, returning to the start on exit."""


@contextlib.contextmanager
def open_backend(workspace_dir: str, jj_workspace: jj.Workspace | None) -> Iterator[Backend]:
    """Open the backend for one command, jj when jj.operation yielded a workspace."""
    yield GitBackend(workspace_dir) if jj_workspace is None else JjBackend(jj_workspace)


def completion_backend(workspace_dir: str, ignore_jj: bool) -> Backend:
    """The backend for completing a command line, read without jj's lock; raise CommandError if jj fails."""
    if not ignore_jj and jj.is_workspace(workspace_dir):
        return JjBackend(jj.Workspace.load(workspace_dir, '@'))
    return GitBackend(workspace_dir)
