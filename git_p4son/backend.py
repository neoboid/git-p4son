"""The backend: what a command asks of the tool holding the change."""

import contextlib
from collections.abc import Callable, Iterator
from typing import ContextManager, Protocol

from . import jj
from .git_backend import GitBackend
from .jj_backend import JjBackend


class Backend(Protocol):
    """What varies between the tools a change can live in."""

    @property
    def revision(self) -> str | None:
        """The commit to read the change from, or None for git's HEAD and index."""

    def check_clean(self) -> None:
        """Raise CommandError, after listing them, if the change has uncommitted work."""

    def checkouts(self) -> ContextManager[Callable[[str], None]]:
        """Yield a function that puts a commit's files in the work tree, returning to the start on exit."""


@contextlib.contextmanager
def open_backend(workspace_dir: str, jj_workspace: jj.Workspace | None) -> Iterator[Backend]:
    """Open the backend for one command, jj when jj.operation yielded a workspace."""
    yield GitBackend(workspace_dir) if jj_workspace is None else JjBackend(jj_workspace)
