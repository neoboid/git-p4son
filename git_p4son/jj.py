"""Colocated jj workspace selection and operation boundaries."""

import contextlib
import json
import os
import re
import shlex
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Self

from .common import CommandError, RunResult, normalize_workspace_path, run
from .git import (
    EOL_FOR_AUTOCRLF, added_files, case_only, case_only_ignored, checkout_detached, is_ancestor,
    get_autocrlf, get_changed_files, get_untracked_files, reset_files_commands,
)
from .jj_lock import working_copy_lock
from .log import log, plural
from .sync_branch import DEFAULT_SYNC_BRANCH, get_sync_branch_setting
from .toml_writer import format_string


_LOCK_ENV = '_GIT_P4SON_JJ_LOCK'
USE_GIT_HINT = ('Put --backend git before the command to use git instead, '
                'or set backend = "git" under [core] in .git-p4son/config.toml.')
_REVISION_TEMPLATE = ('json(self) ++ "\\t" ++ empty ++ "\\t" ++ conflict'
                      ' ++ "\\t" ++ divergent ++ "\\n"')


def check_invoked_inside_workspace(cwd: str) -> None:
    """Raise CommandError when jj -R named a workspace that cwd is outside, then drop the variable for hooks.

    jj util exec, behind the jj p4son alias, sets JJ_WORKSPACE_ROOT and leaves the current directory as it was."""
    root = os.environ.pop('JJ_WORKSPACE_ROOT', None)
    if root is not None and normalize_workspace_path(os.path.realpath(cwd), os.path.realpath(root)) is None:
        raise CommandError(
            'Run jj p4son from inside the workspace; -R is not supported.')


def is_workspace(workspace_dir: str) -> bool:
    """Whether a Git workspace also contains jj metadata."""
    return (Path(workspace_dir) / '.jj').exists()


_JJ = ['jj', '--no-pager', '--color', 'never']


def _run(workspace_dir: str, *command: str, operation: str = '@') -> RunResult[list[str]]:
    """Run jj at operation without snapshotting the working copy."""
    return run([*_JJ, '--ignore-working-copy', '--at-op', operation, *command], cwd=workspace_dir)


def _run_live(workspace_dir: str, *command: str) -> RunResult[list[str]]:
    """Run jj on the live working copy, snapshotting it, as only the steps outside the lock may."""
    return run([*_JJ, *command], cwd=workspace_dir)


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


# The first jj with working-copy locking.
_MIN_VERSION = (0, 41)
MIN_VERSION = '.'.join(map(str, _MIN_VERSION))
# Aliases as tables, with a doc.
_ALIAS_DOC_VERSION = (0, 42)
# util exec neither locks nor snapshots.
_P4SON_ALIAS = '["util", "exec", "--", "git-p4son"]'


def _version(workspace_dir: str) -> tuple[int, ...]:
    """The installed jj's (major, minor), or () if it prints none; raise OSError if jj cannot be run."""
    match = re.search(r'jj (\d+)\.(\d+)',
                      run(['jj', '--version'], cwd=workspace_dir).stdout[0])
    return () if match is None else tuple(map(int, match.groups()))


def _supported(workspace_dir: str) -> bool:
    """Whether the installed jj is _MIN_VERSION or newer; raise OSError if jj cannot be run."""
    return _version(workspace_dir) >= _MIN_VERSION


def is_supported(workspace_dir: str) -> bool:
    """Whether jj is installed and MIN_VERSION or newer."""
    try:
        return _supported(workspace_dir)
    except (OSError, CommandError):
        return False


def has_p4son_alias(workspace_dir: str) -> bool:
    """Whether jj's config defines the jj p4son alias, or any alias of that name."""
    result = run([*_JJ, '--ignore-working-copy', 'config', 'get', 'aliases.p4son'],
                 cwd=workspace_dir, fail_on_returncode=False)
    return result.returncode == 0


def add_p4son_alias(workspace_dir: str) -> None:
    """Add the jj p4son alias to jj's user config, with a doc for jj's completion where jj supports one."""
    alias = _P4SON_ALIAS
    if _version(workspace_dir) >= _ALIAS_DOC_VERSION:
        alias = f'{{ definition = {_P4SON_ALIAS}, doc = "Run git-p4son" }}'
    _run_live(workspace_dir, '--ignore-working-copy', 'config',
              'set', '--user', 'aliases.p4son', alias)


def _matching_eol(workspace_dir: str) -> str:
    """The working-copy.eol-conversion that matches git's core.autocrlf."""
    return EOL_FOR_AUTOCRLF[get_autocrlf(workspace_dir)]


def _auto_track(workspace_dir: str, paths: list[str]) -> str | None:
    """jj's snapshot.auto-track, as it is, leaving paths out too; None if it leaves them all out already."""
    current = run([*_JJ, '--ignore-working-copy', 'config', 'get', 'snapshot.auto-track'],
                  cwd=workspace_dir).stdout[0]
    # A whole fileset, so root:X isn't taken for part of root:X2.
    filesets = [fileset for fileset in map(_root_fileset, paths)
                if not re.search(rf'(?<![^\s(]){re.escape(fileset)}(?![^\s)])', current)]
    return ' ~ '.join([f'({current})', *filesets]) if filesets else None


def _config_value(value: str) -> str:
    """value as jj config set and --config take it: as it is, unless it holds a quote, which needs TOML quoting."""
    return format_string(value) if '"' in value else value


def _command_line(args: list[str]) -> str:
    """args as a command line that cmd, PowerShell and POSIX shells read alike, where double quotes can do it."""
    def quoted(arg: str) -> str:
        if re.fullmatch(r'[\w/.:=@%+,-]+', arg):
            return arg
        return shlex.quote(arg) if re.search(r'["$`\\!]', arg) else f'"{arg}"'
    return ' '.join(map(quoted, args))


def _colocate_commands(workspace_dir: str) -> list[list[str]]:
    """The jj commands that colocate, with jj's settings matching git's from the first snapshot on.

    The settings are the line endings, and leaving out what git ignores only without case."""
    settings = [('working-copy.eol-conversion', _matching_eol(workspace_dir))]
    auto_track = _auto_track(workspace_dir, case_only_ignored(workspace_dir))
    if auto_track is not None:
        settings.append(('snapshot.auto-track', _config_value(auto_track)))
    # jj snapshots as it initializes, before a repo setting could exist.
    init = [arg for name, value in settings for arg in (
        '--config', f'{name}={value}')]
    return [[*init, 'git', 'init', '--colocate'],
            *(['config', 'set', '--repo', name, value] for name, value in settings)]


def colocate(workspace_dir: str) -> None:
    """Make the git repository at workspace_dir a colocated jj workspace too, as _colocate_commands sets it up."""
    for command in _colocate_commands(workspace_dir):
        _run_live(workspace_dir, *command)
    Workspace.load(workspace_dir, '@').check_ignored()


def colocate_steps(workspace_dir: str) -> list[str]:
    """The command lines that colocate by hand, as colocate does."""
    return [_command_line(['jj', *command]) for command in _colocate_commands(workspace_dir)]


def leave_out_case_only_ignored(workspace_dir: str) -> None:
    """Leave the untracked paths git ignores only without case out of jj's snapshots, as jj would track them."""
    paths = case_only_ignored(workspace_dir)
    auto_track = _auto_track(workspace_dir, paths) if paths else None
    if auto_track is None:
        return
    log.heading(
        "Leaving what git ignores only without case out of jj's snapshots")
    _run_live(workspace_dir, '--ignore-working-copy', 'config', 'set', '--repo', 'snapshot.auto-track',
              _config_value(auto_track))
    log.success(f'snapshot.auto-track is now {auto_track}')


def _under(file: str, path: str) -> bool:
    """Whether file is path, or in it when path is a folder ending in /."""
    return file == path or (path.endswith('/') and file.startswith(path))


def _root_fileset(path: str) -> str:
    """The jj fileset that names path, a folder with what it holds, from the workspace root; quoted only if needed."""
    path = path.rstrip('/')
    if re.fullmatch(r'[\w/.-]+', path):
        return f'root:{path}'
    quoted = path.replace('\\', '\\\\').replace('"', '\\"')
    return f'root:"{quoted}"'


def _validate_version(workspace_dir: str) -> None:
    """Raise CommandError unless jj is installed and MIN_VERSION or newer, with a local working copy."""
    try:
        supported = _supported(workspace_dir)
    except OSError as error:
        raise CommandError(
            f'This is a jj workspace, but jj could not be run: {error}. {USE_GIT_HINT}') from error
    if not supported:
        raise CommandError(
            f'jj support needs jj {MIN_VERSION} or newer, for its working-copy locking. {USE_GIT_HINT}')
    kind = Path(workspace_dir) / '.jj' / 'working_copy' / 'type'
    if not kind.is_file() or kind.read_text() != 'local':
        raise CommandError('Only local jj working copies are supported')


@dataclass
class Revision:
    """A jj revision and the git commit it is."""
    change_id: str
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
        return cls(data['change_id'], data['commit_id'], data['parents'], data['description'],
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

    def only_bookmark(self) -> str | None:
        """The one unconflicted local bookmark at the tip, or None for none or several."""
        names = self.bookmarks_at(self.tip.commit)
        if len(names) != 1 or len(self.bookmarks[names[0]]) != 1:
            return None
        return names[0]

    def insert_empty_after(self, commit: str, message: str) -> None:
        """Insert an empty change right after commit, which jj rebases the descendants onto.

        When commit is the working change's, a new empty working change goes on top, so the inserted change is the
        tip's; otherwise the working change stays. It leaves the files alone, so it runs under git-p4son's lock; load
        the workspace again after."""
        edit = commit == self.working.commit
        run([*_JJ, '--ignore-working-copy', 'new', *([] if edit else ['--no-edit']), '--insert-after', commit,
             '-m', message], cwd=self.directory)
        if edit:
            # A new working change on top, so the next edits go into a change of their own, not the catch-up.
            run([*_JJ, '--ignore-working-copy', 'new'], cwd=self.directory)

    def child(self, commit: str) -> str:
        """The commit of the one change whose parent is commit; raise CommandError if there are none or several."""
        children = self.run(
            'log', '-r', f'children({commit})', '--no-graph', '-T', 'commit_id ++ "\\n"').stdout
        if len(children) != 1:
            raise CommandError(
                f'{commit[:7]} has {len(children)} children in jj, where one was expected.')
        return children[0]

    def change_ids(self, commits: list[str]) -> list[str]:
        """The change IDs of commits, in their order."""
        if not commits:
            return []
        lines = self.run('log', '-r', ' | '.join(commits), '--no-graph',
                         '-T', 'commit_id ++ " " ++ change_id ++ "\n"').stdout
        change_of = dict(line.split(' ', 1) for line in lines)
        return [change_of[commit] for commit in commits]

    def existing_changes(self, change_ids: list[str]) -> set[str]:
        """The change_ids jj still has."""
        if not change_ids:
            return set()
        revset = ' | '.join(
            f'present({change})' for change in change_ids)
        return set(self.run('log', '-r', revset, '--no-graph', '-T', 'change_id ++ "\\n"').stdout)

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

    def check_line_endings(self) -> None:
        """Warn, naming the fix, when git's core.autocrlf and jj's working-copy.eol-conversion disagree."""
        matching = _matching_eol(self.directory)
        if matching == self.eol_conversion:
            return
        log.warning(f"jj's working-copy.eol-conversion is {self.eol_conversion}, but git's core.autocrlf is "
                    f'{get_autocrlf(self.directory)}, so jj and git write line endings differently. To match git, run: '
                    f'jj config set --repo working-copy.eol-conversion {matching}')

    def check_ignored(self) -> None:
        """Warn, naming the fix, about the files the tip adds that git ignores only by matching without case."""
        # The tip, as jj new or jj commit right after the snapshot leaves the files in @-.
        if self._on_sync_branch(self.tip.commit):
            return
        added = added_files(self.tip.commit, self.directory)
        files = case_only(added, self.directory)
        if not files:
            return
        # The tip's files, with these untracked, so a folder git lists as a whole holds no file git tracks.
        found = case_only_ignored(self.directory, (self.tip.commit, files))
        paths = list(dict.fromkeys(
            next((path for path in found if _under(file, path)), file) for file in files))
        auto_track = _auto_track(self.directory, paths)
        log.warning(f'jj tracks {plural(len(files), "file")} that git ignores only because of core.ignorecase, '
                    "as jj matches .gitignore with case. To stop tracking them, leave them out of jj's "
                    'snapshot.auto-track, then untrack them:')
        if auto_track is not None:
            log.info(_command_line(
                ['jj', 'config', 'set', '--repo', 'snapshot.auto-track', _config_value(auto_track)]))
        log.info(_command_line(
            ['jj', 'file', 'untrack', *map(_root_fileset, paths)]))

    def _on_sync_branch(self, commit: str) -> bool:
        """Whether commit is in the sync bookmark's history, which git made, not a jj snapshot."""
        targets = self.bookmarks.get(get_sync_branch_setting(
            self.directory) or DEFAULT_SYNC_BRANCH, [])
        return len(targets) == 1 and targets[0] is not None and is_ancestor(commit, targets[0], self.directory)

    def check_clean(self) -> None:
        """Raise CommandError, after listing them, if jj left files git doesn't ignore out of its snapshot, or files
        changed since."""
        log.heading('Checking jj workspace files')
        files = get_untracked_files(self.working.commit, self.directory)
        if files:
            for path in files:
                log.file_change(path, 'untracked')
            raise CommandError('jj did not snapshot the files above. '
                               'Track or ignore them first.')
        self.check_unchanged()
        log.success('All files snapshotted, none changed since')

    def check_unchanged(self) -> None:
        """Raise CommandError, after listing them, if tracked files changed since jj's snapshot."""
        changed = get_changed_files(
            self.working.commit, self.directory, self.eol_conversion)
        if changed:
            for path in changed:
                log.file_change(path, 'modify')
            raise CommandError(
                'The files above changed after jj snapshotted the working copy. Run the command again.')

    def checkout(self, commit: str) -> None:
        """Put commit's files on disk, as jj writes line endings; the working change keeps its content in jj."""
        checkout_detached(commit, self.directory, self.eol_conversion)

    def detach_head(self, commit: str) -> None:
        """Point git's HEAD at commit, detached, so git's commits move no bookmark; the files stay as they are."""
        run(['git', 'update-ref', '--no-deref', 'HEAD', commit], cwd=self.directory)

    def restore_working(self, head: str) -> None:
        """Undo checkout: git's HEAD and index back at head, as jj keeps them, and the working change's files back."""
        for command in self._restore_commands(head):
            run(command, cwd=self.directory)

    def restore_hint(self, head: str) -> list[str]:
        """The commands restore_working runs, for the user to run when it failed."""
        return [shlex.join(command) for command in self._restore_commands(head)]

    def _restore_commands(self, head: str) -> list[list[str]]:
        return [['git', 'update-ref', '--no-deref', 'HEAD', head],
                *reset_files_commands(self.working.commit, head, self.eol_conversion)]

    def move_bookmark(self, name: str, commit: str, old: str) -> None:
        """Move bookmark name from old to commit through git, which jj imports once the lock is released."""
        run(['git', 'update-ref',
            f'refs/heads/{name}', commit, old], cwd=self.directory)

    # The steps below run jj on the live working copy, after git-p4son has released the lock.

    def rebase_branch(self, start: str, destination: str) -> None:
        """Rebase start's branch, its ancestors not in destination and their descendants, onto destination."""
        _run_live(self.directory, 'rebase', '-b', start, '-d', destination)

    def conflicted_since(self, commit: str) -> list[str]:
        """The descendants of commit with conflicts, as "change ID, first line" lines."""
        return _run_live(self.directory, 'log', '-r', f'{commit}:: & conflicts()', '--no-graph',
                         '-T', 'change_id.short() ++ " " ++ description.first_line() ++ "\\n"').stdout

    def working_is_empty(self) -> bool:
        return _run_live(self.directory, 'log', '-r', '@', '--no-graph', '-T', 'empty').stdout == ['true']

    def return_to(self, change: str, editing: bool) -> None:
        """Make change the working change if editing, else start a new one above it."""
        _run_live(self.directory, 'edit' if editing else 'new', change)


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
    log.heading('Opening jj workspace')
    _validate_version(workspace_dir)
    _validate_workspace(workspace_dir)
    # Check before snapshotting, so a busy workspace fails rather than waits.
    with working_copy_lock(workspace_dir):
        pass
    if snapshot:
        snapshot_commit = _run_live(
            workspace_dir, 'log', '-r', '@', '--no-graph', '-T', 'commit_id').stdout[0]
    with working_copy_lock(workspace_dir):
        workspace = Workspace.load(workspace_dir, '@')
        workspace.check_line_endings()
        if snapshot:
            workspace.check_ignored()
        if snapshot and workspace.working.commit != snapshot_commit:
            raise CommandError(
                'The jj working change changed while git-p4son took its lock. Run the command again.')
        log.success(
            f'Change {workspace.tip.change_id[:8]} at operation {workspace.operation[:12]}')
        with _marked_as_locked(root, inherited):
            yield workspace
    if snapshot:
        log.heading('Reconciling jj workspace')
        _run_live(workspace_dir, 'status')
        log.success('jj snapshotted the working copy')
