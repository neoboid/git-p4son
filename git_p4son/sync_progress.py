"""The sync progress: what an interrupted sync has left to do, kept in .git-p4son/sync-progress.toml."""

import contextlib
import datetime
import os
import time
import tomllib
from dataclasses import dataclass, field
from typing import Any, NamedTuple

from . import CONFIG_DIR, __version__
from .common import CommandError, normalize_workspace_path
from .config import ensure_config_dir
from .perforce import P4SyncPreviewFile
from .sync_merge import ChangedFile, WritableSyncFileSet
from .toml_writer import format_key, format_value

VERSION = 1
# Keeps the commits of the changes made while a sync is interrupted alive, until the sync finishes.
INTERSYNC_REF = 'refs/git-p4son/intersync-changes'


class StaleSyncProgress(CommandError):
    """The sync branch synced past the interrupted sync, which an abort gets out of by dropping the sync progress."""

    def __init__(self) -> None:
        super().__init__('The sync commits above were made after the interrupted sync, so its sync progress is '
                         'stale. Drop it with "git p4son sync --abort".')


# Keeps the commits a resume unwound from the sync branch, until the next sync replaces it.
INTERSYNC_COMMITS_REF = 'refs/git-p4son/intersync-commits'
_REPLACE_ATTEMPTS = 5
FILE_NAME = 'sync-progress.toml'

# The [start] section, which each backend fills: its own keys, then on-sync-branch.
Start = dict[str, str | bool]


@dataclass(frozen=True)
class LocalChange:
    """A local change to merge back, by reference: ours is path at the pre-sync commit, base path at base."""
    path: str
    base: str | None
    binary: bool = False
    added_both: bool = False


@dataclass(frozen=True)
class MergedFile:
    """A local change merged back, with the outcome the end reports it under, such as conflicts."""
    path: str
    outcome: str


class AboveCommit(NamedTuple):
    """A commit above the recorded tip of an interrupted sync: the commit, how to get it back, and its subject."""
    commit: str
    back: str
    subject: str


@dataclass(frozen=True)
class IntersyncFile:
    """A file changed while the sync was interrupted, and what its change is measured from: "have", the have revision's
    content its save keeps, or a commit's."""
    path: str
    base: str


@dataclass
class IntersyncSave:
    """The changes made while the sync was interrupted, saved in commit: each file's content under ours/, and under
    have/ the have revision's, once complete."""
    commit: str
    files: list[IntersyncFile]
    complete: bool = False


@dataclass
class PassLists:
    """What a pass found before its p4 sync, as the resume of its interruption needs it; paths as stored."""
    # Every file the preview listed, as (mode, path), the mode 'add', 'upd' or 'del'.
    files: list[tuple[str, str]] = field(default_factory=list)
    changed: list[LocalChange] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    always_writable: list[str] = field(default_factory=list)


@dataclass
class ProgressPass:
    """A pass still to run; subject is a target pass's commit subject, lists the interrupted pass's."""
    changelist: int
    label: str
    exact: bool = False
    subject: str | None = None
    lists: PassLists | None = None


@dataclass
class SyncProgress:
    """What a sync has left to do, the interrupted pass first, and what its end needs from the passes made."""
    sync_branch: str
    depot_root: str
    tip: str
    pre_sync_commit: str
    start: Start
    passes: list[ProgressPass]
    # The files the passes made since the last commit synced, as (mode, path): a catch-up pass's, until a commit.
    uncommitted: list[tuple[str, str]] = field(default_factory=list)
    # The commits a resume unwound from above the tip, named again at the end.
    unwound: list[AboveCommit] = field(default_factory=list)
    changed: list[LocalChange] = field(default_factory=list)
    merged: list[MergedFile] = field(default_factory=list)
    # Merged after the local changes, oldest first.
    intersync: list[IntersyncSave] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    # Filled in writable mode only.
    synced: list[str] = field(default_factory=list)
    written_by: str = __version__
    started: datetime.datetime = field(
        default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0))
    version: int = VERSION


class ProgressRecorder:
    """Keeps a sync's progress on disk as its passes run, from the first pass's p4 sync to the end."""

    def __init__(self, progress: SyncProgress, workspace_dir: str, writable_mode: bool) -> None:
        self.progress = progress
        self.workspace_dir = workspace_dir
        self.writable_mode = writable_mode

    def before_sync(self, preview: list[P4SyncPreviewFile], prep: WritableSyncFileSet) -> None:
        """Record the lists of the first pass left, before its p4 sync changes the files they were read from."""
        self.progress.passes[0].lists = PassLists(
            files=[(entry.mode, self._stored(entry.filepath))
                   for entry in preview],
            changed=[self._change(changed) for changed in prep.changed],
            ignored=self._all_stored(prep.ignored), always_writable=self._all_stored(prep.always_writable))
        self._write()

    def pass_made(self, prep: WritableSyncFileSet, tip: str | None = None) -> None:
        """Drop the first pass left, once its p4 sync finished, or with tip once its commit is made, keeping what
        the end needs from it: its lists, and from prep what it synced."""
        progress = self.progress
        lists = progress.passes.pop(0).lists
        assert lists is not None, 'before_sync comes first'
        by_path = {change.path: change for change in progress.changed} | {
            change.path: change for change in lists.changed}
        progress.changed = list(by_path.values())
        progress.ignored = _added(progress.ignored, lists.ignored)
        if self.writable_mode:
            progress.synced = _added(
                progress.synced, self._all_stored(prep.synced))
        if tip is None:
            modes = {path: mode for mode, path in progress.uncommitted}
            modes.update((path, mode) for mode, path in lists.files)
            progress.uncommitted = [(mode, path)
                                    for path, mode in modes.items()]
        else:
            progress.tip = tip
            progress.uncommitted = []
        self._write()

    def merged(self, filepath: str, outcome: str | None) -> None:
        """Move the local change to filepath, once it is merged back, to the outcomes the end reports, unless it has
        none."""
        path = self._stored(filepath)
        self.progress.changed = [
            change for change in self.progress.changed if change.path != path]
        if outcome is not None:
            self.progress.merged.append(MergedFile(path, outcome))
        self._write()

    def intersync_merged(self, filepath: str, outcome: str | None) -> None:
        """Drop filepath from the oldest save that holds it, once its change is merged back, and the save once empty,
        keeping the outcome, as for a local change."""
        path = self._stored(filepath)
        if outcome is not None:
            self.progress.merged.append(MergedFile(path, outcome))
        for save in self.progress.intersync:
            if any(file.path == path for file in save.files):
                save.files = [file for file in save.files if file.path != path]
                break
        self.progress.intersync = [
            save for save in self.progress.intersync if save.files]
        self._write()

    def finish(self) -> None:
        """Remove the sync progress, once the end has nothing left to do."""
        remove_sync_progress(self.workspace_dir)

    def _write(self) -> None:
        write_sync_progress(self.progress, self.workspace_dir)

    def _stored(self, filepath: str) -> str:
        return normalize_workspace_path(filepath, self.workspace_dir, allow_outside=True) or filepath

    def _all_stored(self, filepaths: list[str]) -> list[str]:
        return [self._stored(filepath) for filepath in filepaths]

    def _change(self, changed: ChangedFile) -> LocalChange:
        return LocalChange(self._stored(changed.filepath), changed.base_commit, changed.is_binary, changed.added_both)


class NoProgress:
    """A recorder that keeps no sync progress, for cherry-pick."""

    def before_sync(self, preview: list[P4SyncPreviewFile], prep: WritableSyncFileSet) -> None:
        pass

    def pass_made(self, prep: WritableSyncFileSet, tip: str | None = None) -> None:
        pass

    def merged(self, filepath: str, outcome: str | None) -> None:
        pass

    def intersync_merged(self, filepath: str, outcome: str | None) -> None:
        pass

    def finish(self) -> None:
        pass


def _added(paths: list[str], more: list[str]) -> list[str]:
    return list(dict.fromkeys([*paths, *more]))


def local_path(path: str, workspace_dir: str) -> str:
    """The local path of a path as the sync progress stores it."""
    return os.path.join(workspace_dir, *path.split('/'))


def progress_path(workspace_dir: str) -> str:
    return os.path.join(workspace_dir, CONFIG_DIR, FILE_NAME)


def write_sync_progress(progress: SyncProgress, workspace_dir: str) -> None:
    """Write progress to a temporary file and rename it into place, so a crash never leaves half a file."""
    ensure_config_dir(workspace_dir)
    path = progress_path(workspace_dir)
    temporary = path + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as f:
        f.write(format_sync_progress(progress))
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            # Windows refuses while another process, such as a virus scanner, has the file open.
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.1)


def read_sync_progress(workspace_dir: str) -> SyncProgress | None:
    """The sync progress an interrupted sync left, or None when there is none.

    Raises CommandError for sync progress a newer git-p4son wrote."""
    try:
        with open(progress_path(workspace_dir), 'rb') as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return None
    if data['version'] > VERSION:
        raise CommandError(f'The interrupted sync\'s progress in {CONFIG_DIR}/{FILE_NAME} was written by git-p4son '
                           f'{data["written-by"]}, which is newer than this one. Upgrade git-p4son to finish it.')
    end = data.get('end', {})
    return SyncProgress(
        sync_branch=data['sync-branch'], depot_root=data['depot-root'], tip=data['tip'],
        pre_sync_commit=data['pre-sync-commit'], start=data['start'],
        passes=[_read_pass(item) for item in data.get('passes', [])],
        uncommitted=[(mode, path)
                     for mode, path in data.get('uncommitted', [])],
        unwound=[AboveCommit(*entry) for entry in data.get('unwound', [])],
        changed=[_read_change(item) for item in end.get('changed', [])],
        merged=[MergedFile(item['path'], item['outcome'])
                for item in end.get('merged', [])],
        intersync=[IntersyncSave(item['commit'], [IntersyncFile(file['path'], file['base']) for file in item['files']],
                                 item.get('complete', False)) for item in data.get('intersync', [])],
        ignored=end.get('ignored', []), synced=end.get('synced', []),
        written_by=data['written-by'], started=data['started'], version=data['version'])


def check_no_interrupted_sync(workspace_dir: str) -> None:
    """Raise CommandError while an interrupted sync waits to be finished, for a command that works on the files."""
    if os.path.exists(progress_path(workspace_dir)):
        raise CommandError('The files on disk are partly synced, as an interrupted sync is not finished. Run '
                           '"git p4son sync" to finish it first.')


def remove_sync_progress(workspace_dir: str) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.remove(progress_path(workspace_dir))


def format_sync_progress(progress: SyncProgress) -> str:
    """The TOML of progress, with a line per list item, for a user stuck mid-sync to read."""
    lines = [_line('version', progress.version), _line('written-by', progress.written_by),
             _line('started', progress.started), _line(
                 'sync-branch', progress.sync_branch),
             _line('depot-root', progress.depot_root), _line('tip', progress.tip),
             _line('pre-sync-commit', progress.pre_sync_commit),
             *_array('uncommitted', [list(entry)
                     for entry in progress.uncommitted]),
             *_array('unwound', [list(entry) for entry in progress.unwound]),
             # Only the end is left; an array of tables can't say it has none.
             *([] if progress.passes else [_line('passes', [])]),
             '', '[start]', *(_line(key, value)
                              for key, value in progress.start.items()),
             '', '[end]', *_array('changed', [_change(change)
                                  for change in progress.changed]),
             *_array('merged', [{'path': merged.path, 'outcome': merged.outcome}
                     for merged in progress.merged]),
             *_array('ignored', progress.ignored),
             *_array('synced', progress.synced)]
    for sync_pass in progress.passes:
        lines += ['', '[[passes]]',
                  _line('changelist', sync_pass.changelist), _line('label', sync_pass.label)]
        if sync_pass.exact:
            lines.append(_line('exact', True))
        if sync_pass.subject is not None:
            lines.append(_line('subject', sync_pass.subject))
        if sync_pass.lists is not None:
            lists = sync_pass.lists
            lines += [*_array('files', [list(entry) for entry in lists.files]),
                      *_array('changed', [_change(change)
                              for change in lists.changed]),
                      *_array('ignored', lists.ignored),
                      *_array('always-writable', lists.always_writable)]
    for save in progress.intersync:
        lines += ['', '[[intersync]]', _line('commit', save.commit), _line('complete', save.complete),
                  *_array('files', [{'path': file.path, 'base': file.base} for file in save.files])]
    return '\n'.join(lines) + '\n'


def _line(key: str, value: object) -> str:
    return f'{format_key(key)} = {format_value(value)}'


def _array(key: str, items: list[Any]) -> list[str]:
    if not items:
        return [f'{format_key(key)} = []']
    return [f'{format_key(key)} = [', *(f'    {format_value(item)},' for item in items), ']']


def _change(change: LocalChange) -> dict[str, str | bool]:
    item: dict[str, str | bool] = {'path': change.path}
    if change.base is not None:
        item['base'] = change.base
    if change.binary:
        item['binary'] = True
    if change.added_both:
        item['added-both'] = True
    return item


def _read_change(item: dict[str, Any]) -> LocalChange:
    return LocalChange(item['path'], item.get('base'), item.get('binary', False), item.get('added-both', False))


def _read_pass(item: dict[str, Any]) -> ProgressPass:
    lists = None
    if 'files' in item:
        lists = PassLists(
            files=[(mode, path) for mode, path in item['files']],
            changed=[_read_change(change)
                     for change in item.get('changed', [])],
            ignored=item.get('ignored', []), always_writable=item.get('always-writable', []))
    return ProgressPass(item['changelist'], item['label'], item.get('exact', False), item.get('subject'), lists)
