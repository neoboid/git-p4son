"""The git adapter for the backend interface."""

import contextlib
from collections.abc import Callable, Iterator

from .changelist_store import alias_for
from .common import CommandError, RunError
from .git import get_current_branch, get_head_commit, get_head_subject, switch_branch, switch_detached
from .lib import check_git_workspace_clean
from .log import log
from .words import Words

DEFAULT_BASE = 'HEAD~1'


class GitBackend:
    """The git backend."""

    words = Words(tool='git', branch='branch', message_source='HEAD commit')
    revision: str | None = None

    def __init__(self, workspace_dir: str) -> None:
        self.workspace_dir = workspace_dir

    def resolve_base(self, base: str | None) -> str:
        """The base as given, or HEAD~1."""
        return DEFAULT_BASE if base is None else base

    def suggested_alias(self) -> str:
        """The alias the current branch suggests; raise CommandError on a detached HEAD."""
        branch = get_current_branch(self.workspace_dir)
        if not branch:
            raise CommandError('Cannot resolve the branch keyword on a detached '
                               'HEAD. Name the alias or changelist explicitly.')
        return alias_for(branch)

    def subject(self) -> str:
        """The HEAD commit's subject; raise CommandError if there is no commit."""
        subject = get_head_subject(self.workspace_dir)
        if subject is None:
            raise CommandError('No commits found to derive message from')
        return subject

    def check_clean(self) -> None:
        """Raise CommandError, after listing them, if the workspace has uncommitted changes."""
        check_git_workspace_clean(self.workspace_dir)

    def base_candidates(self) -> list[tuple[str, str]]:
        """The __branch__ marker, which the completion scripts expand to git's branches."""
        return [('__branch__', '')]

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
