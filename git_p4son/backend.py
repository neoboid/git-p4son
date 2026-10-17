"""The backend: what a command asks of the tool holding the change."""

import contextlib
from collections.abc import Callable, Iterator
from typing import ContextManager, Protocol

from .git_backend import GitBackend


class Backend(Protocol):
    """What varies between the tools a change can live in."""

    def checkouts(self) -> ContextManager[Callable[[str], None]]:
        """Yield a function that puts a commit's files in the work tree, returning to the start on exit."""


@contextlib.contextmanager
def open_backend(workspace_dir: str) -> Iterator[Backend]:
    """Open the backend for one command."""
    yield GitBackend(workspace_dir)
