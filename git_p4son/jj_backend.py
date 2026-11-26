"""The jj adapter for the backend interface, delegating to a jj.Workspace."""

import contextlib
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import NoReturn

from .changelist_store import alias_for
from .common import CommandError, RunError, restoring
from .git import (
    commit_all, files_changed_since, get_head_commit, get_local_changes, is_ancestor, is_sync_subject, merges_above,
)
from .jj import Workspace, changes_revset, leave_out_case_only_ignored
from .sync_progress import GIVE_UP, AboveCommit, StaleSyncProgress
from .log import log
from .sync_branch import choose_sync_branch, sync_branch_name
from .words import (
    Words, base_below_sync_tip, base_not_below, has_merge, named_base, no_sync_branch, not_on_sync_tip, several_nearest,
)


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
    # Set by finish_sync, for after_lock: where the sync bookmark is now, and whether to rebase.
    finished: tuple[str, bool] | None = None
    # A resume's: the undescribed changes jj made on the recorded commit, holding the files as Perforce left them.
    snapshots: list[str] = field(default_factory=list)
    # A resume's: the changes above the recorded commit, oldest first, as (commit, change ID, description), and the
    # commit the bookmark was found at.
    above: list[tuple[str, str, str]] = field(default_factory=list)
    bookmark_at: str = ''

    @property
    def return_command(self) -> str:
        return f'jj {"edit" if self.editing else "new"} {self.start}'


def _refuse_bookmark_at_working(workspace: Workspace, bookmark: str) -> NoReturn:
    """Raise CommandError, listing the working change's files, as sync would bury the change in the bookmark."""
    log.heading(f'Checking sync bookmark {bookmark}')
    move_back = f'jj bookmark set {bookmark} -r @- --allow-backwards'
    error = f'jj bookmark "{bookmark}" points at the working change, which a sync would bury in its history.'
    working = workspace.working
    if working.empty:
        raise CommandError(f'{error} Move it back first: {move_back}')
    changes = get_local_changes(
        working.parents[0], workspace.directory, working.commit)
    for path in changes.adds:
        log.file_change(path, 'add')
    for path in changes.mods:
        log.file_change(path, 'modify')
    for path in changes.dels:
        log.file_change(path, 'delete')
    for old, new in changes.moves:
        log.file_change(f'{old} -> {new}', 'move')
    raise CommandError(f'{error} Keep the files above out of {bookmark} with: {move_back}. '
                       'Or keep them as a change: jj describe, then jj new.')


def _unconflicted(name: str, targets: list[str | None]) -> str:
    """The one commit bookmark name points at; raise CommandError for a conflicted one."""
    if len(targets) != 1 or targets[0] is None:
        raise CommandError(
            f'jj bookmark "{name}" is conflicted. Resolve it first.')
    return targets[0]


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

    def implicit_base(self) -> str:
        """The nearest bookmark below the change.

        Raises CommandError, saying -b is required, for none or several."""
        workspace = self.workspace
        names = workspace.nearest_bookmarks()
        if not names:
            raise CommandError(f'-b is required, as no bookmark is below the change. Name the base it starts from, '
                               f'such as {sync_branch_name(workspace.directory)}.')
        if len(names) > 1:
            raise several_nearest(names, 'the change', self.words)
        return names[0]

    def suggested_alias(self) -> str:
        """The alias the one bookmark at the tip suggests, else the first 8 letters of its change ID."""
        bookmark = self.workspace.only_bookmark()
        # Change IDs are letters k to z, so a prefix is always a valid alias.
        return self.workspace.tip.change_id[:8] if bookmark is None else alias_for(bookmark)

    def change_id(self) -> str:
        return self.workspace.tip.change_id

    def base_name(self, base: str) -> str:
        """The bookmark base names, or the one bookmark at the change it names, else that change by its ID; raise
        CommandError for a change with several."""
        workspace = self.workspace
        if base in workspace.bookmarks:
            return base
        commit = self.resolve_base(base)
        names = workspace.bookmarks_at(commit)
        if not names:
            # It resolves the change's current commit after a rebase, and fails for a gone or divergent one.
            return changes_revset([workspace.resolve(commit).change_id])
        return named_base(base, self.words, lambda _: False, lambda _: names)

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

    def nearest_change(self, change_ids: list[str]) -> str | None:
        return self.workspace.nearest_change(change_ids)

    def subject(self) -> str:
        """The first line of the change's description; raise CommandError if it has none."""
        lines = self.workspace.tip.description.splitlines()
        if not lines:
            raise CommandError('The jj change has no description. '
                               'Describe it with "jj describe", or pass -m or -F.')
        return lines[0]

    def check_clean(self) -> None:
        """Raise CommandError, listing them, if files changed since jj's snapshot, or jj left any out of it."""
        self.workspace.check_clean()

    def check_on_sync_tip(self) -> None:
        """Raise CommandError unless the sync bookmark exists, unconflicted, and the change contains its tip, with no
        merge above it."""
        workspace = self.workspace
        sync = sync_branch_name(workspace.directory)
        log.heading(f'Checking for the latest {sync}')
        if workspace.sync_bookmark() is None:
            raise no_sync_branch(sync, self.words)
        tip = _unconflicted(sync, workspace.bookmarks[sync])
        if not is_ancestor(tip, workspace.tip.commit, workspace.directory):
            raise not_on_sync_tip('The change', sync, self.words)
        merges = merges_above(tip, workspace.tip.commit, workspace.directory)
        if merges:
            raise has_merge('The change', merges[0], sync, self.words)
        log.success('Based on it')

    def check_base(self, base: str, name: str, recorded: bool = False) -> None:
        """Raise CommandError, calling it name, unless base is below the change and contains the sync bookmark's
        tip."""
        workspace = self.workspace
        sync = sync_branch_name(workspace.directory)
        tip = _unconflicted(sync, workspace.bookmarks[sync])
        # The sync bookmark's commit is below the change once check_on_sync_tip passed, and contains itself.
        if tip == base:
            return
        if not is_ancestor(tip, base, workspace.directory):
            raise base_below_sync_tip(
                name, sync, self.words, name in workspace.bookmarks)
        if not is_ancestor(base, workspace.tip.commit, workspace.directory):
            raise base_not_below(name, 'the change', self.words, recorded)

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
            log.heading(
                f'Returning to jj change {workspace.working.change_id[:8]}')
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

        Raises CommandError when it does not exist, is conflicted, or points at the working change."""
        workspace = self.workspace
        name = choose_sync_branch(
            workspace.directory, branch, lambda name: name in workspace.bookmarks, 'bookmark')
        commit = _unconflicted(name, workspace.bookmarks[name])
        if commit == workspace.working.commit:
            _refuse_bookmark_at_working(workspace, name)
        self._sync = _Sync(name, commit, workspace.tip.change_id[:12], workspace.working is workspace.tip,
                           get_head_commit(workspace.directory), workspace.tip.commit == commit)
        return commit

    def start_sync(self) -> None:
        """Put the sync bookmark's files on disk; on the bookmark they are there already, and HEAD only detaches."""
        sync = self._prepared()
        if sync.on_sync_branch:
            self.workspace.detach_head(sync.commit)
            return
        log.heading(f'Checking out sync bookmark {sync.bookmark}')
        self.workspace.checkout(sync.commit)
        log.success(f'From {sync.start}')

    def resume_sync(self, sync_branch: str, tip: str, start: dict[str, str | bool], pass_subject: str | None) -> str:
        """Check the sync bookmark is at tip, or at a commit on it with pass_subject, the crash window, and the working
        change above it, and take the start from start; described changes above tip are for the caller to sort.

        Raises CommandError, naming the way out, where it isn't."""
        workspace = self.workspace
        log.heading(f'Checking sync bookmark {sync_branch}')
        if sync_branch not in workspace.bookmarks:
            raise no_sync_branch(sync_branch, self.words)
        at = _unconflicted(sync_branch, workspace.bookmarks[sync_branch])
        # jj's commit_sync moves the bookmark once the commit is made, so a crash leaves it on the pass's commit.
        moved_by_pass = False
        if at != tip and pass_subject is not None:
            revision = workspace.resolve(at)
            moved_by_pass = revision.parents == [tip] and revision.description.split('\n', 1)[
                0] == pass_subject
        if at != tip and not moved_by_pass:
            if is_ancestor(tip, at, workspace.directory):
                synced = [description for _, _, description in workspace.changes_above(tip, at)
                          if is_sync_subject(description)]
                if synced:
                    for description in synced:
                        log.info(description)
                    raise StaleSyncProgress()
            raise CommandError(f'jj bookmark "{sync_branch}" moved since the interrupted sync left it at {tip[:12]}. '
                               f'Put it back with "jj bookmark set {sync_branch} -r {tip} --allow-backwards", then run '
                               f'"git p4son sync" again, {GIVE_UP}.')
        if not is_ancestor(tip, workspace.working.commit, workspace.directory):
            children = workspace.children(tip)
            back = (' or '.join(f'"jj edit {change[:12]}"' for change in children) if children
                    else f'"jj new {tip[:12]}"')
            raise CommandError(f'The working change is not above {sync_branch}, where the interrupted sync left its '
                               f'files. Return to them with {back}, then run "git p4son sync" again, {GIVE_UP}.')
        above = workspace.changes_above(tip)
        log.success(f'At {tip[:12]}')
        self._sync = _Sync(sync_branch, tip, str(start['change']), bool(start['edited']),
                           get_head_commit(workspace.directory), bool(
                               start['on-sync-branch']),
                           snapshots=[change for _, change, _ in above], above=above, bookmark_at=at)
        return tip

    def commits_above(self, tip: str) -> list[AboveCommit]:
        """The described changes above tip, oldest first, each with a jj show of its commit, which still resolves once
        the change is abandoned."""
        return [AboveCommit(commit, f'jj show {commit[:12]}', description)
                for commit, _, description in self._prepared().above if description]

    def resume_at(self, commit: str) -> None:
        """Put the sync bookmark and git's HEAD at commit, its change kept from the snapshots after_lock abandons."""
        sync = self._prepared()
        if sync.bookmark_at != commit:
            self.workspace.move_bookmark(sync.bookmark, commit, sync.commit)
        sync.commit = commit
        own = {change for above, change, _ in sync.above if above == commit}
        sync.snapshots = [
            snapshot for snapshot in sync.snapshots if snapshot not in own]
        # jj keeps git's HEAD at the working change's parent; moving it there changes no file.
        self.workspace.detach_head(commit)

    def returns_to(self) -> str | None:
        sync = self._prepared()
        return None if sync.on_sync_branch else sync.start

    def files_changed_since(self, commit: str) -> set[str]:
        return files_changed_since(commit, self.workspace.directory, self.workspace.eol_conversion)

    def progress_start(self) -> tuple[str, dict[str, str | bool]]:
        """The change sync started from, and whether it was the working change itself."""
        sync = self._prepared()
        return sync.bookmark, {'backend': 'jj', 'change': sync.start, 'edited': sync.editing,
                               'on-sync-branch': sync.on_sync_branch}

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

    def fail_sync(self, resumable: bool) -> None:
        """Exclude what jj's next snapshot would wrongly track, and say where the sync stays."""
        sync = self._prepared()
        leave_out_case_only_ignored(self.workspace.directory)
        if resumable:
            back = '' if sync.on_sync_branch else f', which rebases your changes and returns to {sync.start}'
            log.warning(f'Staying on {sync.bookmark}: jj makes a new working change there, holding the files as '
                        f'Perforce left them. Run "git p4son sync" to finish it{back}, {GIVE_UP}. After a "jj edit" '
                        'elsewhere, return to that working change with "jj edit" first.')
        elif not sync.on_sync_branch:
            log.warning(f'Staying on {sync.bookmark}: jj makes a new working change there, holding the files as '
                        f'Perforce left them, and nothing is rebased. Your changes are as they were; return with '
                        f'"{sync.return_command}" once it is sorted out.')

    def finish_sync(self, state: str, rebase: bool) -> None:
        """Exclude what jj would wrongly track; the rebase and the return wait for after_lock, outside jj's lock."""
        self._prepared().finished = (state, rebase)
        leave_out_case_only_ignored(self.workspace.directory)

    def after_lock(self) -> None:
        """After a sync, rebase the changes it started from onto the sync bookmark, then return to them.

        Raises CommandError, after returning, if the rebase failed; the sync stays."""
        sync = self._sync
        if sync is None or sync.finished is None:
            return
        workspace = self.workspace
        # A snapshot change still holds the files as Perforce left them; once jj is past it, nothing needs it.
        workspace.abandon_unless_working(sync.snapshots)
        if sync.on_sync_branch:
            return
        at, rebase = sync.finished
        if not workspace.change_exists(sync.start):
            log.warning(
                f'Staying on {sync.bookmark}, as {sync.start}, where sync started, no longer exists.')
            return
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
