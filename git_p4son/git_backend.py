"""The git adapter for the backend interface."""

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .changelist_store import alias_for
from .common import CommandError, RunError, restoring
from .git import (
    author_dates, branch_exists, branches_at, commit_all, get_current_branch, get_dirty_files, get_head_commit,
    get_head_subject, insert_empty_commit, is_ancestor, local_upstream, merge_base, merges_above,
    nearest_branches_below, resolve_commit, switch_branch, switch_detached,
)
from .lib import check_git_workspace_clean
from .sync_branch import choose_sync_branch, sync_branch_name
from .log import log
from .words import Words, base_below_sync_tip, has_merge, named_base, not_on_sync_tip, several_nearest, sync_tip_heading

DEFAULT_BASE = 'HEAD~1'


@dataclass(frozen=True)
class _Position:
    """Where HEAD is: on branch, or detached at commit when branch is None."""
    branch: str | None
    commit: str

    @classmethod
    def current(cls, workspace_dir: str) -> '_Position':
        return cls(get_current_branch(workspace_dir), get_head_commit(workspace_dir))

    @property
    def name(self) -> str:
        return self.branch or self.commit[:7]

    def restore(self, workspace_dir: str) -> None:
        """Check the branch out again, or the commit detached."""
        if self.branch:
            switch_branch(self.branch, workspace_dir)
        else:
            switch_detached(self.commit, workspace_dir)


@dataclass(frozen=True)
class _Sync:
    """A sync onto branch, started at start."""
    branch: str
    start: _Position

    @property
    def on_sync_branch(self) -> bool:
        return self.start.branch == self.branch


class GitBackend:
    """The git backend."""

    words = Words(tool='git', branch='branch', message_source='HEAD commit')
    # HEAD, not its commit, so the per-commit loop's checkouts move it.
    revision = 'HEAD'

    def __init__(self, workspace_dir: str) -> None:
        self.workspace_dir = workspace_dir
        self._sync: _Sync | None = None

    def resolve_base(self, base: str | None) -> str:
        """The base as given, or HEAD~1; raise CommandError if it names no commit."""
        base = DEFAULT_BASE if base is None else base
        resolve_commit(base, self.workspace_dir)
        return base

    def implicit_base(self) -> tuple[str, str]:
        """The current branch's local upstream, else the nearest branch below HEAD, and which of the two it is.

        Raises CommandError, saying -b is required, where neither gives a base whose commits since are the change's."""
        workspace_dir = self.workspace_dir
        branch = get_current_branch(workspace_dir)
        base = None if branch is None else local_upstream(
            branch, workspace_dir)
        if base is None:
            return self._nearest_branch_below(branch or 'HEAD'), 'nearest branch below'
        sync_branch = sync_branch_name(workspace_dir)
        if base == sync_branch:
            # The commits since it leave out what HEAD can't reach; check_on_sync_tip refuses opening files from
            # behind it.
            return base, f'upstream of {branch}'
        # Rewritten or moved on, the commits since it would take in its old ones.
        if not is_ancestor(f'refs/heads/{base}', self.revision, workspace_dir):
            raise CommandError(f'-b is required, as {base}, the upstream of {branch}, is not below it, such as after '
                               f'{base} was amended or rebased. Name the base, or rebase {branch} onto {base}.')
        if not branch_exists(sync_branch, workspace_dir):
            return base, f'upstream of {branch}'
        sync_ref = f'refs/heads/{sync_branch}'
        fix = f'Name the base, or move the upstream, such as with "git branch -u {sync_branch}".'
        if is_ancestor(f'refs/heads/{base}', sync_ref, workspace_dir):
            raise CommandError(f'-b is required, as {base}, the upstream of {branch}, is in the history of '
                               f'{sync_branch}, such as when left on an old sync commit. {fix}')
        # A merge of the sync branch above the upstream brings sync commits into the commits since it.
        if not is_ancestor(merge_base(self.revision, sync_ref, workspace_dir), f'refs/heads/{base}', workspace_dir):
            raise CommandError(f'-b is required, as {base}, the upstream of {branch}, would take in commits of '
                               f'{sync_branch}, such as after a merge of it. {fix}')
        return base, f'upstream of {branch}'

    def _nearest_branch_below(self, change: str) -> str:
        """The one branch on the nearest commit below HEAD that has any, above the sync branch, else the sync branch.

        Raises CommandError, saying -b is required, for several, one below a merge of the sync branch, or no sync
        branch."""
        workspace_dir = self.workspace_dir
        sync_branch = sync_branch_name(workspace_dir)
        if not branch_exists(sync_branch, workspace_dir):
            raise CommandError(f'-b is required, as {change} has no local upstream and there is no branch '
                               f'{sync_branch} to find the base above. Name the base the change starts from.')
        sync_ref = f'refs/heads/{sync_branch}'
        names = nearest_branches_below(self.revision, sync_ref, workspace_dir)
        if not names:
            return sync_branch
        if len(names) > 1:
            raise several_nearest(names, change, self.words)
        # As for an upstream: a merge of the sync branch above the base brings sync commits into the commits since it.
        fork = merge_base(self.revision, sync_ref, workspace_dir)
        if not is_ancestor(fork, f'refs/heads/{names[0]}', workspace_dir):
            raise CommandError(f'-b is required, as {names[0]}, the nearest branch below {change}, would take in '
                               f'commits of {sync_branch}, such as after a merge of it. Name the base, or rebase '
                               f'{change} onto {sync_branch}.')
        return names[0]

    def suggested_alias(self) -> str:
        """The alias the current branch suggests; raise CommandError on a detached HEAD."""
        branch = get_current_branch(self.workspace_dir)
        if not branch:
            raise CommandError('Cannot resolve the branch keyword on a detached '
                               'HEAD. Name the alias or changelist explicitly.')
        return alias_for(branch)

    def change_id(self) -> None:
        return None

    def base_name(self, base: str) -> str:
        """The branch base names, or the one branch at the commit it names; raise CommandError otherwise."""
        workspace_dir = self.workspace_dir
        return named_base(base, self.words, lambda name: branch_exists(name, workspace_dir),
                          lambda base: branches_at(resolve_commit(base, workspace_dir), workspace_dir))

    def commit_identities(self, commits: list[str]) -> list[str]:
        """The author dates, which git keeps through rebases, amends and squashes."""
        return author_dates(commits, self.workspace_dir)

    def insert_empty_after(self, commit: str, message: str) -> str:
        """Insert it with git's plumbing, recreating the commits after it and moving the branch."""
        inserted, old_head, stale = insert_empty_commit(
            commit, message, self.workspace_dir)
        for branch in stale:
            log.warning(f'{branch} still builds on the commits before "{message}". Rebase it onto them with '
                        f'"git rebase --onto HEAD {old_head[:12]} {branch}".')
        return inserted

    def existing_changes(self, change_ids: list[str]) -> set[str]:
        return set(change_ids)

    def nearest_change(self, change_ids: list[str]) -> None:
        return None

    def subject(self) -> str:
        """The HEAD commit's subject; raise CommandError if there is no commit."""
        subject = get_head_subject(self.workspace_dir)
        if subject is None:
            raise CommandError('No commits found to derive message from')
        return subject

    def check_clean(self) -> None:
        """Raise CommandError, after listing them, if the workspace has uncommitted changes."""
        check_git_workspace_clean(self.workspace_dir)

    def check_on_sync_tip(self, commits: list[str] | None = None) -> None:
        """Raise CommandError unless HEAD contains the sync branch's tip, with no merge above it, or unless each of
        commits contains it; nothing to check without the branch."""
        workspace_dir = self.workspace_dir
        sync_branch = sync_branch_name(workspace_dir)
        log.heading(sync_tip_heading(sync_branch, commits))
        if not branch_exists(sync_branch, workspace_dir):
            log.success(f'No {self.words.branch} {sync_branch} yet')
            return
        sync_ref = f'refs/heads/{sync_branch}'
        for commit in commits or ['HEAD']:
            if not is_ancestor(sync_ref, commit, workspace_dir):
                change = f'Commit {commit[:7]}' if commits else self._change()
                raise not_on_sync_tip(change, sync_branch, self.words)
        # A per-commit run comes here a second time, with its commits, after the merges were looked for.
        merges = [] if commits else merges_above(
            sync_ref, 'HEAD', workspace_dir)
        if merges:
            raise has_merge(self._change(), merges[0], sync_branch, self.words)
        log.success('Based on it')

    def check_base_on_sync_tip(self, base: str, name: str) -> None:
        """Raise CommandError, calling it name, unless base contains the sync branch's tip; nothing to check without
        the branch."""
        workspace_dir = self.workspace_dir
        sync_branch = sync_branch_name(workspace_dir)
        if (branch_exists(sync_branch, workspace_dir)
                and not is_ancestor(f'refs/heads/{sync_branch}', base, workspace_dir)):
            raise base_below_sync_tip(
                name, sync_branch, self.words, branch_exists(name, workspace_dir))

    def _change(self) -> str:
        """The name of the change in a refusal: the current branch, or HEAD when detached."""
        return get_current_branch(self.workspace_dir) or 'HEAD'

    def base_candidates(self) -> list[tuple[str, str]]:
        """The __branch__ marker, which the completion scripts expand to git's branches."""
        return [('__branch__', '')]

    @contextlib.contextmanager
    def checkouts(self) -> Iterator[Callable[[str], None]]:
        """Yield a function that checks out a commit detached, returning to the starting branch or commit on exit."""
        start = _Position.current(self.workspace_dir)

        def restore() -> None:
            log.heading(f'Returning to {start.name}')
            start.restore(self.workspace_dir)
        with restoring(restore,
                       lambda error: log.error(f'Could not return to {start.name}: {error}')):
            yield lambda commit: switch_detached(commit, self.workspace_dir)

    def prepare_sync(self, branch: str | None) -> str:
        """The sync branch: branch, else [sync] branch, else main; raise CommandError if none exists."""
        name = choose_sync_branch(self.workspace_dir, branch,
                                  lambda name: branch_exists(name, self.workspace_dir), 'branch')
        self._sync = _Sync(name, _Position.current(self.workspace_dir))
        # In full, so a tag of the same name, which git would read first, is not read instead.
        return f'refs/heads/{name}'

    def start_sync(self) -> None:
        """Check out the sync branch, unless sync started on it."""
        sync = self._prepared()
        if sync.on_sync_branch:
            return
        log.heading(f'Checking out sync branch {sync.branch}')
        switch_branch(sync.branch, self.workspace_dir)
        log.success(f'From {sync.start.name}')

    def commit_sync(self, subject: str) -> int:
        return commit_all(subject, self.workspace_dir)

    def abandon_sync(self) -> None:
        """A failure to return is logged, so the error that stopped the sync propagates."""
        sync = self._prepared()
        if sync.on_sync_branch:
            return
        try:
            self._return(sync)
        except RunError as error:
            log.error(f'Could not return to {sync.start.name}: {error}')

    def fail_sync(self) -> None:
        sync = self._prepared()
        if not sync.on_sync_branch:
            log.warning(f'Staying on {sync.branch}, with the files as Perforce left them. '
                        f'Return to {sync.start.name} once it is sorted out.')

    def finish_sync(self, state: str, rebase: bool) -> None:
        """Return with the git rebase to run, as sync does not rebase; stay while merged local changes are left."""
        sync = self._prepared()
        if sync.on_sync_branch:
            return
        if any(kind != 'untracked' for _, kind in get_dirty_files(self.workspace_dir)):
            log.warning(f'Staying on {sync.branch}, with the local changes above to review and commit. '
                        f'Then return to {sync.start.name}.')
            return
        self._return(sync)
        log.info(
            f'{sync.branch} is now at {state}. Run "git rebase {sync.branch}" to bring {sync.start.name} up to date.')

    def after_lock(self) -> None:
        """Nothing to do: git's sync returns under the lock."""

    def _prepared(self) -> _Sync:
        assert self._sync is not None, 'prepare_sync comes first'
        return self._sync

    def _return(self, sync: _Sync) -> None:
        log.heading(f'Returning to {sync.start.name}')
        sync.start.restore(self.workspace_dir)
        log.success(sync.start.name)
