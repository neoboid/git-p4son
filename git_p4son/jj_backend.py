"""The jj adapter for the backend interface, delegating to a jj.Workspace."""

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from .changelist_store import alias_for
from .common import CommandError, RunError, restoring
from .git import commit_all, get_head_commit
from .jj import Workspace, exclude_case_only_ignored
from .log import log
from .sync_branch import choose_sync_branch
from .words import Words, named_base


@dataclass
class _Sync:
    """A sync onto bookmark, from the change start, which editing says was the working change itself."""
    bookmark: str
    # The bookmark's commit, which each sync commit moves on.
    commit: str
    start: str
    editing: bool
    # git's HEAD before the checkout, which jj keeps at the working change's parent.
    head: str
    on_sync_branch: bool
    # Set by finish_sync, for after_lock: the changelist synced to, and whether to rebase.
    finished: tuple[int | None, bool] | None = None

    @property
    def return_command(self) -> str:
        return f'jj {"edit" if self.editing else "new"} {self.start}'


class JjBackend:
    """The jj backend, over a workspace loaded at one jj operation."""

    words = Words(tool='jj', branch='bookmark',
                  message_source='jj change description')

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace
        self._sync: _Sync | None = None
        # For a per-commit step; change_id, the alias and the subject stay the tip's.
        self._checked_out: str | None = None

    @property
    def revision(self) -> str:
        """The commit a per-commit step checked out, else the tip."""
        return self._checked_out or self.workspace.tip.commit

    def resolve_base(self, base: str | None) -> str:
        """The base resolved to a commit, None for the tip's parent; raise CommandError on conflicts."""
        # -b's documented default is git's HEAD~1, which users type; jj has no HEAD, so it means the default here.
        return self.workspace.resolve_base(None if base == 'HEAD~1' else base)

    def suggested_alias(self) -> str:
        """The alias the one bookmark at the tip suggests, else the first 8 letters of its change ID."""
        bookmark = self.workspace.only_bookmark()
        # Change IDs are letters k to z, so a prefix is always a valid alias.
        return self.workspace.tip.change_id[:8] if bookmark is None else alias_for(bookmark)

    def change_id(self) -> str:
        return self.workspace.tip.change_id

    def base_name(self, base: str) -> str:
        """The bookmark base names, or the one bookmark at the commit it names; raise CommandError otherwise."""
        workspace = self.workspace
        return named_base(base, self.words, lambda name: name in workspace.bookmarks,
                          lambda base: workspace.bookmarks_at(self.resolve_base(base)))

    def commit_identities(self, commits: list[str]) -> list[str]:
        """The change IDs, which survive rebases and rewording."""
        return self.workspace.change_ids(commits)

    def insert_empty_after(self, commit: str, message: str) -> str:
        """Insert it with jj, which moves the bookmarks too, then load the workspace at the operation that made it."""
        self.workspace.insert_empty_after(commit, message)
        self.workspace = Workspace.load(self.workspace.directory, '@')
        # jj rebased every child of commit onto the new change, so it's the only one left.
        return self.workspace.child(commit)

    def existing_changes(self, change_ids: list[str]) -> set[str]:
        return self.workspace.existing_changes(change_ids)

    def subject(self) -> str:
        """The first line of the change's description; raise CommandError if it has none."""
        lines = self.workspace.tip.description.splitlines()
        if not lines:
            raise CommandError('The jj change has no description. '
                               'Describe it with "jj describe", or pass -m or -F.')
        return lines[0]

    def check_clean(self) -> None:
        """Raise CommandError, listing them, if jj left files out of its snapshot; a step's checkout leaves none."""
        if self._checked_out is None:
            self.workspace.check_snapshot()

    def base_candidates(self) -> list[tuple[str, str]]:
        """The local bookmarks."""
        return [(name, 'jj bookmark') for name in self.workspace.bookmarks]

    @contextlib.contextmanager
    def checkouts(self) -> Iterator[Callable[[str], None]]:
        """Yield a function that puts a commit's files on disk with git, putting the working change's back on exit.

        Raises CommandError if tracked files changed since jj's snapshot, which the checkouts would overwrite."""
        workspace = self.workspace
        workspace.check_unchanged()
        head = get_head_commit(workspace.directory)

        def checkout(commit: str) -> None:
            workspace.checkout(commit)
            self._checked_out = commit

        def restore() -> None:
            self._checked_out = None
            workspace.restore_working(head)

        def report(error: RunError) -> None:
            log.error(
                f"Could not put the working change's files back: {error}")
            log.info(
                'Run these before any jj command, so jj does not snapshot the files of another commit:')
            for command in workspace.restore_hint(head):
                log.info(command)
        with restoring(restore, report):
            yield checkout

    def prepare_sync(self, branch: str | None) -> str:
        """The sync bookmark: branch, else [sync] branch, else main; returns its commit.

        Raises CommandError when it does not exist, or is conflicted."""
        workspace = self.workspace
        name = choose_sync_branch(
            workspace.directory, branch, lambda name: name in workspace.bookmarks, 'bookmark')
        targets = workspace.bookmarks[name]
        if len(targets) != 1 or targets[0] is None:
            raise CommandError(
                f'jj bookmark "{name}" is conflicted. Resolve it first.')
        commit = targets[0]
        self._sync = _Sync(name, commit, workspace.tip.change_id[:12], workspace.working is workspace.tip,
                           get_head_commit(workspace.directory), workspace.tip.commit == commit)
        return commit

    def check_sync_clean(self) -> None:
        self.workspace.check_clean()

    def start_sync(self) -> None:
        """Put the sync bookmark's files on disk; on the bookmark they are there already, and HEAD only detaches."""
        sync = self._prepared()
        if sync.on_sync_branch:
            self.workspace.detach_head(sync.commit)
            return
        log.heading(f'Checking out sync bookmark {sync.bookmark}')
        self.workspace.checkout(sync.commit)
        log.success(f'From {sync.start}')

    def commit_sync(self, subject: str) -> int:
        """Stage with jj's line endings, and move the sync bookmark to the commit."""
        sync = self._prepared()
        staged = commit_all(subject, self.workspace.directory,
                            self.workspace.eol_conversion)
        commit = get_head_commit(self.workspace.directory)
        self.workspace.move_bookmark(sync.bookmark, commit, sync.commit)
        sync.commit = commit
        return staged

    def abandon_sync(self) -> None:
        """Put the working change's files back, and git's HEAD at its parent, so jj sees no change."""
        sync = self._prepared()
        if sync.on_sync_branch:
            return
        log.heading(f'Returning to {sync.start}')
        self.workspace.restore_working(sync.head)
        log.success(sync.start)

    def fail_sync(self) -> None:
        """Exclude what jj's next snapshot would wrongly track, and say where the sync stays."""
        sync = self._prepared()
        exclude_case_only_ignored(self.workspace.directory)
        if not sync.on_sync_branch:
            log.warning(f'Staying on {sync.bookmark}: jj makes a new working change there, holding the files as '
                        f'Perforce left them, and nothing is rebased. Your changes are as they were; return with '
                        f'"{sync.return_command}" once it is sorted out.')

    def finish_sync(self, changelist: int | None, rebase: bool) -> None:
        """Exclude what jj would wrongly track; the rebase and the return wait for after_lock, outside jj's lock."""
        self._prepared().finished = (changelist, rebase)
        exclude_case_only_ignored(self.workspace.directory)

    def after_lock(self) -> None:
        """After a sync, rebase the changes it started from onto the sync bookmark, then return to them.

        Raises CommandError, after returning, if the rebase failed; the sync stays."""
        sync = self._sync
        if sync is None or sync.finished is None or sync.on_sync_branch:
            return
        changelist, rebase = sync.finished
        workspace = self.workspace
        rebase_command = f'jj rebase -b {sync.start} -d {sync.bookmark}'
        failure = None
        if rebase:
            log.heading(f'Rebasing your changes onto {sync.bookmark}')
            try:
                workspace.rebase_branch(sync.start, sync.commit)
            except RunError as error:
                failure = error
            else:
                self._report_conflicts(sync)
        at = f'CL {changelist}' if changelist else 'the synced changelist'
        if not workspace.working_is_empty():
            log.warning(f'Staying on {sync.bookmark}, with the merged local changes in the working change. Review '
                        f'them, then return with "{sync.return_command}".')
        else:
            log.heading(f'Returning to {sync.start}')
            workspace.return_to(sync.start, sync.editing)
            log.success(sync.start)
            hint = '' if rebase else f' Run "{rebase_command}" to bring your changes up to date.'
            log.info(f'{sync.bookmark} is now at {at}.{hint}')
        if failure is not None:
            raise CommandError(f'{sync.bookmark} reached {at}, but jj could not rebase your changes: {failure}. '
                               f'Run: {rebase_command}', failure.returncode) from failure

    def _report_conflicts(self, sync: _Sync) -> None:
        """List the changes the rebase recorded conflicts in."""
        conflicted = self.workspace.conflicted_since(sync.commit)
        if not conflicted:
            log.success(f'Rebased onto {sync.bookmark}')
            return
        for line in conflicted:
            log.info(line)
        log.warning(
            'jj recorded conflicts in the changes above. Resolve them with jj.')

    def _prepared(self) -> _Sync:
        assert self._sync is not None, 'prepare_sync comes first'
        return self._sync
