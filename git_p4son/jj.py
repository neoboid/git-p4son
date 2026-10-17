"""Colocated jj workspace selection and operation boundaries."""

import contextlib
import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .common import CommandError, RunResult, run
from .git import get_head_commit, get_untracked_files
from .jj_lock import working_copy_lock
from .log import log


_LOCK_ENV = '_GIT_P4SON_JJ_LOCK'
USE_GIT_HINT = ('Put --backend git before the command to use git instead, '
                'or set backend = "git" under [core] in .git-p4son/config.toml.')
_REVISION_TEMPLATE = ('json(self) ++ "\\t" ++ empty ++ "\\t" ++ conflict'
                      ' ++ "\\t" ++ divergent ++ "\\n"')


def is_workspace(workspace_dir: str) -> bool:
    """Whether a Git workspace also contains jj metadata."""
    return (Path(workspace_dir) / '.jj').exists()


def _run(workspace_dir: str, *command: str, operation: str = '@') -> RunResult[list[str]]:
    """Run jj at operation without snapshotting the working copy."""
    return run(['jj', '--no-pager', '--color', 'never',
                '--ignore-working-copy', '--at-op', operation, *command],
               cwd=workspace_dir)


def _operation_id(workspace_dir: str) -> str:
    """The id of jj's current operation."""
    return _run(workspace_dir, 'op', 'log', '-n', '1',
                '--no-graph', '-T', 'id').stdout[0]


def _validate_workspace(workspace_dir: str) -> None:
    """Raise CommandError unless this is a colocated, non-sparse jj workspace with no git operation in progress."""
    root = Path(workspace_dir).resolve()
    repo = root / '.jj' / 'repo'
    if not repo.is_dir():
        raise CommandError('Only colocated jj workspaces are supported')
    target = repo / 'store' / 'git_target'
    if not target.is_file():
        raise CommandError(
            'The jj workspace is not backed by the colocated git repository')
    git_dir = run(['git', 'rev-parse', '--absolute-git-dir'],
                  cwd=workspace_dir).stdout[0]
    if (target.parent / target.read_text()).resolve() != Path(git_dir).resolve():
        raise CommandError(
            'jj and git must share the same colocated repository')
    if _run(workspace_dir, 'sparse', 'list').stdout != ['.']:
        raise CommandError('Sparse jj workspaces are not supported')
    for state in ('rebase-merge', 'rebase-apply', 'MERGE_HEAD', 'CHERRY_PICK_HEAD'):
        if (Path(git_dir) / state).exists():
            raise CommandError(
                'A git operation is in progress. Finish it first, or use --backend git.')


def _validate_version(workspace_dir: str) -> None:
    """Raise CommandError unless jj is installed and 0.41 or newer, with a local working copy."""
    try:
        version = run(['jj', '--version'], cwd=workspace_dir).stdout[0]
    except OSError as error:
        raise CommandError(
            f'This is a jj workspace, but jj could not be run: {error}. {USE_GIT_HINT}') from error
    match = re.search(r'jj (\d+)\.(\d+)', version)
    if not match or tuple(map(int, match.groups())) < (0, 41):
        raise CommandError(
            f'jj support needs jj 0.41 or newer, for its working-copy locking. {USE_GIT_HINT}')
    kind = Path(workspace_dir) / '.jj' / 'working_copy' / 'type'
    if not kind.is_file() or kind.read_text() != 'local':
        raise CommandError('Only local jj working copies are supported')


@dataclass
class Revision:
    """A jj revision and the git commit it is."""
    commit: str
    parents: list[str]
    description: str
    empty: bool
    conflict: bool
    divergent: bool

    @classmethod
    def parse(cls, line: str) -> Self:
        """Parse a line of _REVISION_TEMPLATE output."""
        data, empty, conflict, divergent = line.split('\t')
        data = json.loads(data)
        return cls(data['commit_id'], data['parents'], data['description'],
                   empty == 'true', conflict == 'true', divergent == 'true')


def _resolve(directory: str, operation: str, revision: str) -> Revision:
    """Resolve exactly one jj revision with a git commit and no conflicts or divergence, or raise CommandError."""
    lines = _run(directory, 'log', '-r', revision, '--no-graph',
                 '-T', _REVISION_TEMPLATE, operation=operation).stdout
    if len(lines) != 1:
        raise CommandError(
            f'jj revision "{revision}" names {len(lines)} changes. Name exactly one.')
    resolved = Revision.parse(lines[0])
    if resolved.conflict:
        raise CommandError(
            f'jj revision "{revision}" has conflicts. Resolve them first.')
    if resolved.divergent:
        raise CommandError(
            f'jj revision "{revision}" is divergent. Resolve it first.')
    if not resolved.commit.strip('0'):
        raise CommandError(
            'The jj root revision has no git commit. Choose another base.')
    return resolved


@dataclass
class Workspace:
    """A colocated jj workspace at one operation, with the change git-p4son works on at tip."""
    directory: str
    operation: str
    working: Revision
    tip: Revision
    bookmarks: dict[str, list[str | None]]
    eol_conversion: str
    sync_bookmark: str | None = None

    @classmethod
    def load(cls, directory: str, operation: str) -> Self:
        """Load the workspace at operation.

        tip is the working change, or its parent when the working change is empty and new."""
        if operation == '@':
            operation = _operation_id(directory)
        working = _resolve(directory, operation, '@')
        bookmarks = {}
        result = _run(directory, 'bookmark', 'list', '-T', 'json(self) ++ "\\n"',
                      operation=operation)
        for line in result.stdout:
            bookmark = json.loads(line)
            if 'remote' not in bookmark:
                bookmarks[bookmark['name']] = bookmark['target']
        eol = _run(directory, 'config', 'get', 'working-copy.eol-conversion',
                   operation=operation).stdout[0]
        if eol not in ('none', 'input', 'input-output'):
            raise CommandError(
                f'Unsupported jj working-copy.eol-conversion: {eol}')
        workspace = cls(directory, operation, working, working, bookmarks, eol)
        if (working.empty and not working.description
                and len(working.parents) == 1
                and not workspace.bookmarks_at(working.commit)):
            workspace.tip = workspace.resolve(working.parents[0])
        return workspace

    def run(self, *command: str) -> RunResult[list[str]]:
        """Run jj at this workspace's operation."""
        return _run(self.directory, *command, operation=self.operation)

    def resolve(self, revision: str) -> Revision:
        """Resolve exactly one conflict-free jj revision."""
        return _resolve(self.directory, self.operation, revision)

    def bookmarks_at(self, commit: str) -> list[str]:
        """The local bookmarks pointing at commit."""
        return [name for name, targets in self.bookmarks.items()
                if commit in targets]

    def current_bookmark(self) -> str:
        """Require one unconflicted local bookmark at the selected tip."""
        names = self.bookmarks_at(self.tip.commit)
        if len(names) != 1 or len(self.bookmarks[names[0]]) != 1:
            raise CommandError('The selected jj change does not have exactly one bookmark. '
                               'Name the alias explicitly, or set a bookmark there.')
        return names[0]

    def resolve_base(self, base: str | None) -> str:
        """Resolve the base, None for the tip's parent, and reject conflicted changes in the selected range."""
        if base is None:
            if len(self.tip.parents) != 1:
                raise CommandError(
                    'The selected jj change has several parents. Name the base with -b.')
            base = self.tip.parents[0]
        resolved = self.resolve(base)
        conflicts = self.run('log', '-r',
                             f'({resolved.commit}..{self.tip.commit}) & (conflicts() | divergent())',
                             '--no-graph', '-T', 'commit_id ++ "\\n"').stdout
        if conflicts:
            raise CommandError('The selected jj changes have conflicts or divergence. '
                               'Resolve them first.')
        return resolved.commit

    def check_snapshot(self) -> None:
        """Raise CommandError, after listing them, if jj left files git does not ignore out of its snapshot."""
        log.heading('Checking jj workspace files')
        files = get_untracked_files(self.working.commit, self.directory)
        if files:
            for path in files:
                log.file_change(path, 'untracked')
            raise CommandError('jj did not snapshot the files above. '
                               'Track or ignore them first.')
        log.success('All files snapshotted')

    def prepare_sync(self, bookmark: str | None) -> None:
        """Require an empty working change above the sync bookmark."""
        working = self.working
        if (not working.empty or working.description or len(working.parents) != 1
                or self.bookmarks_at(working.commit)):
            raise CommandError('sync in a jj workspace needs an empty working change with no description or '
                               'bookmark and one parent. Run "jj new <bookmark>" first.')
        bookmark = bookmark or self.current_bookmark()
        if self.bookmarks.get(bookmark) != [self.tip.commit]:
            raise CommandError(
                f'jj bookmark "{bookmark}" must point at the working change\'s parent')
        if get_head_commit(self.directory) != self.tip.commit:
            raise CommandError(
                'git HEAD is not the parent of the jj working change')
        self.sync_bookmark = bookmark

    def detach_for_sync(self) -> None:
        """Keep Git commits from advancing any bookmark other than the selected one."""
        run(['git', 'update-ref', '--no-deref', 'HEAD',
             self.tip.commit, self.tip.commit], cwd=self.directory)

    def advance_sync(self) -> None:
        """Advance the sync bookmark after a completed Git commit."""
        head = get_head_commit(self.directory)
        ref = f'refs/heads/{self.sync_bookmark}'
        run(['git', 'update-ref', ref, head, self.tip.commit],
            cwd=self.directory)
        self.tip.commit = head


@contextlib.contextmanager
def _marked_as_locked(root: str, inherited: str | None) -> Iterator[None]:
    """Mark the workspace at root as under git-p4son's lock for the processes it starts, as hooks."""
    os.environ[_LOCK_ENV] = root
    try:
        yield
    finally:
        if inherited is None:
            os.environ.pop(_LOCK_ENV, None)
        else:
            os.environ[_LOCK_ENV] = inherited


@contextlib.contextmanager
def operation(workspace_dir: str, *, lock_only: bool = False,
              snapshot: bool = True) -> Iterator[Workspace | None]:
    """Yield the jj workspace, holding jj's working-copy lock until the command finishes.

    Yields None outside a jj workspace, and with lock_only, which only takes the lock and needs no jj. A run under
    git-p4son's own lock, as from a hook, reuses it with lock_only and raises CommandError without."""
    if not is_workspace(workspace_dir):
        yield None
        return
    root = str(Path(workspace_dir).resolve())
    inherited = os.environ.get(_LOCK_ENV)
    if inherited == root:
        if not lock_only:
            raise CommandError(
                'git-p4son run under its own jj lock, as from a hook, needs --backend git')
        yield None
        return
    if lock_only:
        with working_copy_lock(workspace_dir), _marked_as_locked(root, inherited):
            yield None
        return
    _validate_version(workspace_dir)
    _validate_workspace(workspace_dir)
    # Check before snapshotting, so a busy workspace fails rather than waits.
    with working_copy_lock(workspace_dir):
        pass
    if snapshot:
        snapshot_commit = run(['jj', '--no-pager', '--color', 'never', 'log', '-r', '@',
                               '--no-graph', '-T', 'commit_id'], cwd=workspace_dir).stdout[0]
    before = _operation_id(workspace_dir)
    with working_copy_lock(workspace_dir):
        if before != _operation_id(workspace_dir):
            raise CommandError(
                'jj changed while git-p4son took its lock. Run the command again.')
        workspace = Workspace.load(workspace_dir, before)
        if snapshot and workspace.working.commit != snapshot_commit:
            raise CommandError(
                'The jj working change changed while git-p4son took its lock. Run the command again.')
        with _marked_as_locked(root, inherited):
            yield workspace
    if snapshot:
        log.heading('Reconciling jj workspace')
        run(['jj', '--no-pager', '--color', 'never', 'status'], cwd=workspace_dir)
        log.success('jj snapshotted the working copy')
