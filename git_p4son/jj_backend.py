"""The jj adapter for the backend interface, delegating to a jj.Workspace."""

from collections.abc import Callable
from typing import ContextManager

from .common import CommandError
from .jj import Workspace


class JjBackend:
    """The jj backend, over a workspace loaded at one jj operation."""

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def revision(self) -> str:
        """The commit holding the change: the working change, or its parent when that is empty and new."""
        return self.workspace.tip.commit

    def check_clean(self) -> None:
        """Raise CommandError, after listing them, if jj left files out of its snapshot."""
        self.workspace.check_snapshot()

    def checkouts(self) -> ContextManager[Callable[[str], None]]:
        """Not supported yet; run_command refuses the per-commit commands in a jj workspace first."""
        raise CommandError(
            'Checking out commits one by one is not supported in a jj workspace yet.')
