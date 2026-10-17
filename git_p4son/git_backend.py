"""The git adapter for the backend interface."""

import contextlib
from collections.abc import Callable, Iterator

from .common import RunError
from .git import get_current_branch, get_head_commit, switch_branch, switch_detached
from .log import log


class GitBackend:
    """The git backend."""

    def __init__(self, workspace_dir: str) -> None:
        self.workspace_dir = workspace_dir

    @contextlib.contextmanager
    def checkouts(self) -> Iterator[Callable[[str], None]]:
        """Yield a function that checks out a commit detached, returning to the starting branch or commit on exit.

        A failure to return after the body raised is logged, so the body's exception propagates."""
        branch = get_current_branch(self.workspace_dir)
        start = get_head_commit(self.workspace_dir)

        def back() -> None:
            if branch:
                switch_branch(branch, self.workspace_dir)
            else:
                switch_detached(start, self.workspace_dir)

        try:
            yield lambda commit: switch_detached(commit, self.workspace_dir)
        except BaseException:
            try:
                back()
            except RunError as error:
                log.error(f'Could not return to {branch or start}: {error}')
            raise
        back()
