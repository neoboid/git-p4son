"""The jj adapter for the backend interface, delegating to a jj.Workspace."""

from collections.abc import Callable
from typing import ContextManager

from .changelist_store import alias_for
from .common import CommandError
from .git import commit_all
from .jj import Workspace
from .lib import check_git_workspace_clean
from .words import Words


class JjBackend:
    """The jj backend, over a workspace loaded at one jj operation."""

    words = Words(tool='jj', branch='bookmark',
                  message_source='jj change description')

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    @property
    def revision(self) -> str:
        """The commit holding the change: the working change, or its parent when that is empty and new."""
        return self.workspace.tip.commit

    def resolve_base(self, base: str | None) -> str:
        """The base resolved to a commit, None for the tip's parent; raise CommandError on conflicts."""
        # -b's documented default is git's HEAD~1, which users type; jj has no HEAD, so it means the default here.
        return self.workspace.resolve_base(None if base == 'HEAD~1' else base)

    def suggested_alias(self) -> str:
        """The alias the one bookmark at the tip suggests; raise CommandError without exactly one."""
        return alias_for(self.workspace.current_bookmark())

    def subject(self) -> str:
        """The first line of the change's description; raise CommandError if it has none."""
        lines = self.workspace.tip.description.splitlines()
        if not lines:
            raise CommandError('The jj change has no description. '
                               'Describe it with "jj describe", or pass -m or -F.')
        return lines[0]

    def check_clean(self) -> None:
        """Raise CommandError, after listing them, if jj left files out of its snapshot."""
        self.workspace.check_snapshot()

    def base_candidates(self) -> list[tuple[str, str]]:
        """The local bookmarks."""
        return [(name, 'jj bookmark') for name in self.workspace.bookmarks]

    def checkouts(self) -> ContextManager[Callable[[str], None]]:
        """Not supported yet; open_backend refuses the per-commit commands in a jj workspace first."""
        raise CommandError(
            'Checking out commits one by one is not supported in a jj workspace yet.')

    def prepare_sync(self, branch: str | None) -> str:
        """The bookmark to move: branch, else the tip's one bookmark; returns the tip's commit."""
        self.workspace.prepare_sync(branch)
        return self.workspace.tip.commit

    def check_sync_clean(self) -> None:
        """Read line endings as jj writes them."""
        check_git_workspace_clean(
            self.workspace.directory, self.workspace.eol_conversion)

    def start_sync(self) -> None:
        """Detach git's HEAD, so its commits move no bookmark but the sync bookmark, which commit_sync moves."""
        self.workspace.detach_for_sync()

    def abandon_sync(self) -> None:
        """Nothing to do: git's HEAD stays detached at the tip, as jj keeps it."""

    def fail_sync(self) -> None:
        """Nothing to say: sync started on the sync bookmark."""

    def finish_sync(self, changelist: int | None) -> None:
        """Nothing to do yet: the sync bookmark has moved, and sync started on it."""

    def commit_sync(self, subject: str) -> int:
        """Stage with jj's line endings, and move the sync bookmark to the commit."""
        staged = commit_all(subject, self.workspace.directory,
                            self.workspace.eol_conversion)
        self.workspace.advance_sync()
        return staged
