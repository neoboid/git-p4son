"""Saving local changes to the files a sync overwrites, and merging them back afterwards."""

import dataclasses
import os
from collections.abc import Callable, Sequence, Set as AbstractSet
from dataclasses import dataclass, field

from .common import normalize_workspace_path
from .git import (
    find_base_commits, get_blob_oids, get_files_at_commits, get_tracked_files,
    merge_file,
)
from .log import log, plural
from .perforce import (
    ClobberMode,
    P4SyncPreviewFile,
    is_always_writable_file_type,
    is_binary_file_type,
    p4_head_types,
)
from .writable import is_user_writable, make_read_only, make_writable

_MERGE_LABELS = ('Perforce', 'base', 'local')
# Told each file's path and the outcome it is reported under, once it is merged back.
OnMerged = Callable[[str, str | None], None]


@dataclass
class ChangedFile:
    """A writable file to merge after sync; a None staged path means git had no version."""
    filepath: str
    base_commit: str | None
    ours_path: str | None
    base_path: str | None
    is_binary: bool = False
    added_both: bool = False


@dataclass(frozen=True)
class ChangedFileRef:
    """A local change to stage: filepath at the pre-sync commit, and at base_commit; added_both means p4 adds it over
    local content."""
    filepath: str
    base_commit: str | None
    is_binary: bool = False
    added_both: bool = False


@dataclass
class WritableSyncFileSet:
    """Writable files found during sync preview, classified."""
    changed: list[ChangedFile] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    always_writable: list[str] = field(default_factory=list)
    # Every file the preview said p4 would update, writable or not.
    synced: list[str] = field(default_factory=list)


def _stage_temp_content(temp_root: str, rel_path: str, suffix: str,
                        content: bytes) -> str:
    """Write content to temp_root/rel_path + suffix and return that path."""
    rel_norm = rel_path.replace('\\', '/')
    temp_path = os.path.join(temp_root, rel_norm + suffix)
    os.makedirs(os.path.dirname(temp_path), exist_ok=True)
    with open(temp_path, 'wb') as f:
        f.write(content)
    return temp_path


def _to_crlf(content: bytes) -> bytes:
    """Convert content to CRLF line endings without doubling existing \\r."""
    return content.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')


def _workspace_endings(content: bytes | None, uses_crlf: bool, binary: bool) -> bytes | None:
    """content, as git holds it, in the workspace's line endings; binary content as it is."""
    return _to_crlf(content) if content is not None and uses_crlf and not binary else content


def _write_file(filepath: str, content: bytes) -> None:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    make_writable([filepath])
    with open(filepath, 'wb') as f:
        f.write(content)


def _stage_changed_file(ref: ChangedFileRef, rel_path: str,
                        ours: bytes | None, base: bytes | None,
                        temp_root: str, uses_crlf: bool) -> ChangedFile:
    """Stage HEAD and baseline content of a changed file, text in the workspace line ending."""
    ours = _workspace_endings(ours, uses_crlf, ref.is_binary)
    base = _workspace_endings(base, uses_crlf, ref.is_binary)

    ours_path = (_stage_temp_content(temp_root, rel_path, '.ours', ours)
                 if ours is not None else None)
    base_path = (_stage_temp_content(temp_root, rel_path, '.base', base)
                 if base is not None else None)
    return ChangedFile(filepath=ref.filepath, base_commit=ref.base_commit,
                       ours_path=ours_path, base_path=base_path,
                       is_binary=ref.is_binary, added_both=ref.added_both)


def stage_changed_files(refs: list[ChangedFileRef], pre_sync_head_commit: str, workspace_dir: str, temp_root: str,
                        uses_crlf: bool) -> list[ChangedFile]:
    """Stage each local change's content at pre_sync_head_commit and at its base commit, for the merge-back."""
    rel_paths = {ref.filepath: os.path.relpath(ref.filepath, workspace_dir)
                 for ref in refs}
    queries = []
    for ref in refs:
        queries.append((pre_sync_head_commit, rel_paths[ref.filepath]))
        if ref.base_commit is not None:
            queries.append((ref.base_commit, rel_paths[ref.filepath]))
    contents = get_files_at_commits(queries, workspace_dir)
    staged = []
    for ref in refs:
        rel = rel_paths[ref.filepath]
        ours = contents[(pre_sync_head_commit, rel)]
        base = None if ref.base_commit is None else contents.get(
            (ref.base_commit, rel))
        staged.append(_stage_changed_file(
            ref, rel, ours, base, temp_root, uses_crlf))
    return staged


def _log_file_group(log_fn: Callable[[str], None], files: list[str], singular: str, plural_noun: str,
                    text: str, workspace_dir: str | None = None) -> None:
    """Log how many files are in a group, then list them if workspace_dir is given."""
    if not files:
        return
    log_fn(f'{plural(len(files), singular, plural_noun)} {text}')
    if workspace_dir is not None:
        for f in files:
            log.info(os.path.relpath(f, workspace_dir))


def _log_prepare_summary(result: WritableSyncFileSet, workspace_dir: str,
                         clobber_mode: ClobberMode,
                         unchanged_count: int = 0) -> None:
    """Log what the writable-file classification found."""
    log.heading('Sync preparation')
    if unchanged_count:
        label = 'file' if unchanged_count == 1 else 'files'
        log.success(
            f'{unchanged_count} writable {label} unchanged, '
            'skipping merge')

    _log_file_group(log.success, result.always_writable,
                    'git-ignored file is', 'git-ignored files are',
                    'always writable (+w), p4 syncs them normally')
    _log_file_group(log.warning, [cf.filepath for cf in result.changed],
                    'file has', 'files have',
                    'local changes, will merge after sync', workspace_dir)

    outcome = ('will be overwritten by p4 (clobber is enabled on the workspace)'
               if clobber_mode is ClobberMode.CLOBBER else 'will not be synced')
    _log_file_group(log.warning, result.ignored, 'git-ignored writable file', 'git-ignored writable files',
                    outcome, workspace_dir)


def prepare_writable_files(preview_files: list[P4SyncPreviewFile],
                           workspace_dir: str,
                           pre_sync_head_commit: str,
                           temp_root: str,
                           uses_crlf: bool,
                           clobber_mode: ClobberMode,
                           changed_locally: AbstractSet[str] = frozenset()
                           ) -> WritableSyncFileSet:
    """Classify the preview's writable files and stage the modified ones for post-sync merge.

    changed_locally holds the repo-relative paths commits other than syncs changed, checked whatever their write bit."""
    result = WritableSyncFileSet()

    log.heading('Detecting writable files')
    writable = []
    added_upstream = set()
    for entry in preview_files:
        if (is_user_writable(entry.filepath) or changed_locally
                and normalize_workspace_path(entry.filepath, workspace_dir) in changed_locally):
            writable.append(entry.filepath)
            if entry.mode == 'add':
                added_upstream.add(entry.filepath)

    log.success(
        f'{len(writable)} of {plural(len(preview_files), "file")} writable or changed locally')
    if not writable:
        return result

    log.heading('Splitting writable files into tracked and ignored')
    # Split on tracking, not ignore patterns: a tracked file matching .gitignore must still sync.
    tracked_set = get_tracked_files(writable, workspace_dir)
    tracked = [f for f in writable if f in tracked_set]
    result.ignored = [f for f in writable if f not in tracked_set]
    log.success(f'{len(tracked)} tracked, {len(result.ignored)} ignored')

    if result.ignored:
        log.heading('Checking ignored files for the +w (always writable) type')
        ignored_types = p4_head_types(result.ignored, workspace_dir)
        ignored = []
        for f in result.ignored:
            head_type = ignored_types.get(f)
            # p4 updates a +w file it has, writable or not, but adds none over a writable file on disk.
            if head_type and is_always_writable_file_type(head_type) and f not in added_upstream:
                result.always_writable.append(f)
            else:
                ignored.append(f)
        result.ignored = ignored
        log.success(f'{len(result.always_writable)} always writable (+w)')

    if not tracked:
        _log_prepare_summary(result, workspace_dir, clobber_mode)
        return result

    # Pass 1: find modified files by comparing blob OIDs, in batched git calls.
    log.heading('Detecting modified tracked writable files')
    if clobber_mode is ClobberMode.NOCLOBBER:
        # noclobber refuses any writable file; the merge restores the modified ones.
        make_read_only(tracked)

    rel_paths = {f: os.path.relpath(f, workspace_dir) for f in tracked}
    candidates = [f for f in tracked if f not in added_upstream]
    base_commits = find_base_commits(
        [rel_paths[f] for f in candidates], pre_sync_head_commit,
        workspace_dir)

    oid_queries = []
    for f in candidates:
        base = base_commits.get(rel_paths[f])
        if base != pre_sync_head_commit:
            oid_queries.append((pre_sync_head_commit, rel_paths[f]))
            if base is not None:
                oid_queries.append((base, rel_paths[f]))
    oids = get_blob_oids(oid_queries, workspace_dir)

    unchanged_count = 0
    metas: list[ChangedFileRef] = []
    for f in tracked:
        if f in added_upstream:
            # p4 adds a file the client never had: always merge, against an empty base.
            metas.append(ChangedFileRef(
                filepath=f, base_commit=None, added_both=True))
            continue

        # The workspace is clean, so HEAD content is the on-disk content.
        rel = rel_paths[f]
        base = base_commits.get(rel)
        if base == pre_sync_head_commit:
            unchanged_count += 1
            continue
        if base is not None:
            ours_oid = oids.get((pre_sync_head_commit, rel))
            if ours_oid is not None and ours_oid == oids.get((base, rel)):
                unchanged_count += 1
                continue
        elif oids.get((pre_sync_head_commit, rel)) is None:
            # Every local change is in the pre-sync commit: a file it lacks is one an earlier pass of this sync added.
            unchanged_count += 1
            continue

        metas.append(ChangedFileRef(filepath=f, base_commit=base))

    log.success(f'{len(metas)} changed, {unchanged_count} unchanged')

    # Pass 2: file types of the changed files. Pass 3: stage their content.
    if metas:
        log.heading('Finding file types (text/binary)')
        head_types = p4_head_types(
            [m.filepath for m in metas], workspace_dir)
        binary = {m.filepath for m in metas
                  if is_binary_file_type(head_types.get(m.filepath) or '')}
        log.success(f'{len(metas) - len(binary)} text, {len(binary)} binary')

        log.heading('Staging tracked changed files for post-sync merge')
        result.changed = stage_changed_files(
            [dataclasses.replace(m, is_binary=m.filepath in binary)
             for m in metas],
            pre_sync_head_commit, workspace_dir, temp_root, uses_crlf)
        log.success(f'{plural(len(metas), "file")} staged')

    _log_prepare_summary(result, workspace_dir, clobber_mode, unchanged_count)
    return result


def _empty_base(temp_root: str) -> str:
    """A shared empty file, the base when no baseline commit exists."""
    path = os.path.join(temp_root, '.empty_base')
    if not os.path.exists(path):
        open(path, 'wb').close()
    return path


def _merge_back(cf: ChangedFile, workspace_dir: str, temp_root: str) -> str | None:
    """Merge cf's local change back into the workspace; the outcome it is reported under, None for none."""
    filepath = cf.filepath
    rel_path = os.path.relpath(filepath, workspace_dir)
    log.info(f'{rel_path}: base = {cf.base_commit or "(none)"}')

    # Deleted upstream: let the delete stand, local edits stay in git history.
    if not os.path.exists(filepath):
        return 'deleted-upstream' if cf.ours_path is not None else None

    if cf.ours_path is None:
        return 'deleted-locally'

    if cf.is_binary:
        with open(cf.ours_path, 'rb') as f:
            _write_file(filepath, f.read())
        return 'binary'

    clean, merged = merge_file(filepath, cf.base_path or _empty_base(temp_root), cf.ours_path,
                               _MERGE_LABELS)
    _write_file(filepath, merged)

    if clean:
        return 'clean'
    return 'added-both' if cf.added_both else 'conflicts'


def merge_changed_files(changed_files: list[ChangedFile], workspace_dir: str, temp_root: str,
                        on_merged: OnMerged | None = None,
                        merged_before: list[tuple[str, str]] | None = None) -> None:
    """Merge local changes back into the workspace after syncing, calling on_merged with each file's path and the
    outcome it is reported under after it.

    merged_before holds the (path, outcome) of files an interrupted run merged back, reported with these."""
    if not changed_files and not merged_before:
        return

    log.heading('Merging local changes')

    merged: list[tuple[str, str | None]] = [*(merged_before or [])]
    for cf in changed_files:
        outcome = _merge_back(cf, workspace_dir, temp_root)
        merged.append((cf.filepath, outcome))
        if on_merged is not None:
            on_merged(cf.filepath, outcome)

    _report_outcomes(_by_outcome(merged, workspace_dir), workspace_dir)


def _by_outcome(merged: Sequence[tuple[str, str | None]], workspace_dir: str) -> dict[str | None, list[str]]:
    """The merged files under their outcomes, each file once: under its last outcome that leaves something to look at,
    else its last, as a later clean merge carries an earlier one's conflict markers along."""
    quiet = ('clean', None)
    last: dict[str, tuple[str, str | None]] = {}
    for path, outcome in merged:
        key = normalize_workspace_path(
            path, workspace_dir, allow_outside=True) or path
        if outcome not in quiet or key not in last or last[key][1] in quiet:
            last[key] = (last[key][0] if key in last else path, outcome)
    outcomes: dict[str | None, list[str]] = {}
    for path, outcome in last.values():
        outcomes.setdefault(outcome, []).append(path)
    return outcomes


# Each merge-back outcome, with how its files are logged: at warning unless clean, under a count and a sentence.
_OUTCOMES = {
    'clean': ('file', 'files', 'merged successfully'),
    'conflicts': ('file', 'files', 'merged with conflicts'),
    'added-both': ('file was', 'files were',
                   'added both locally and in Perforce - no common baseline, conflict markers show both full versions'),
    'binary': ('binary file has', 'binary files have', 'local changes, local version restored'),
    'deleted-upstream': ('file', 'files',
                         'deleted in Perforce but modified locally, local edits available via git history'),
    'deleted-locally': ('file', 'files', 'deleted locally but modified in Perforce'),
    'restored': ('file', 'files', 'deleted in Perforce but changed while the sync was interrupted, that version '
                 'restored'),
    'deleted-changed': ('file', 'files', 'deleted while the sync was interrupted but changed since, kept'),
}


def _report_outcomes(outcomes: dict[str | None, list[str]], workspace_dir: str) -> None:
    """Log the merged files under their outcomes, then what to do next."""
    for outcome, (singular, plural_noun, text) in _OUTCOMES.items():
        _log_file_group(log.success if outcome == 'clean' else log.warning, outcomes.get(outcome, []), singular,
                        plural_noun, text, workspace_dir)
    if not any(outcomes.get(outcome) for outcome in _OUTCOMES):
        return
    log.info('')
    if outcomes.get('conflicts') or outcomes.get('added-both'):
        log.info('Manually review changes, resolve conflicts and commit when ready')
    else:
        log.info('Manually review changes and commit when ready')


@dataclass(frozen=True)
class IntersyncChange:
    """A change made while a sync was interrupted: filepath as the user left it (ours) and what they edited (base),
    in git's line endings, None where there was no file."""
    filepath: str
    ours: bytes | None
    base: bytes | None


def _read(filepath: str) -> bytes | None:
    try:
        with open(filepath, 'rb') as f:
            return f.read()
    except FileNotFoundError:
        return None


def _merge_intersync(change: IntersyncChange, workspace_dir: str, temp_root: str, uses_crlf: bool) -> str | None:
    """Apply change onto the file as it now stands; the outcome it is reported under, None for no change."""
    # Sniffed, as a file the user added has no Perforce file type to tell.
    binary = any(b'\0' in content for content in (
        change.ours, change.base) if content)
    ours = _workspace_endings(change.ours, uses_crlf, binary)
    base = _workspace_endings(change.base, uses_crlf, binary)
    if ours == base:
        return None
    filepath = change.filepath
    current = _read(filepath)
    if ours is None:
        if current is None:
            return 'clean'
        if current != base:
            return 'deleted-changed'
        make_writable([filepath])
        os.remove(filepath)
        return 'clean'
    if current is None:
        _write_file(filepath, ours)
        return 'clean' if base is None else 'restored'
    if binary:
        _write_file(filepath, ours)
        return 'binary'
    rel_path = os.path.relpath(filepath, workspace_dir)
    ours_path = _stage_temp_content(
        temp_root, rel_path, '.intersync.ours', ours)
    base_path = (_empty_base(temp_root) if base is None
                 else _stage_temp_content(temp_root, rel_path, '.intersync.base', base))
    clean, merged = merge_file(filepath, base_path, ours_path, _MERGE_LABELS)
    _write_file(filepath, merged)
    return 'clean' if clean else 'conflicts'


def merge_intersync_changes(changes: Sequence[IntersyncChange], workspace_dir: str, temp_root: str, uses_crlf: bool,
                            on_merged: OnMerged, merged_before: list[tuple[str, str]] | None = None) -> None:
    """Merge the changes made while a sync was interrupted back onto the files as they now stand, in order, calling
    on_merged with each file's path after it.

    merged_before holds the (path, outcome) of the changes an interrupted run merged back, reported with these."""
    if not changes and not merged_before:
        return
    log.heading('Merging the changes made while the sync was interrupted')
    merged: list[tuple[str, str | None]] = [*(merged_before or [])]
    for change in changes:
        outcome = _merge_intersync(change, workspace_dir, temp_root, uses_crlf)
        merged.append((change.filepath, outcome))
        on_merged(change.filepath, outcome)
    _report_outcomes(_by_outcome(merged, workspace_dir), workspace_dir)


def report_not_synced(preps: list[WritableSyncFileSet],
                      clobber_mode: ClobberMode, workspace_dir: str) -> None:
    """Log the git-ignored writable files p4 refused or overwrote."""
    files = sorted({f for p in preps for f in p.ignored})
    if not files:
        return
    if clobber_mode is ClobberMode.CLOBBER:
        heading, outcome = 'Git-ignored writable files overwritten by p4 (clobber enabled)', 'overwritten'
    else:
        heading, outcome = 'Files not synced (git-ignored and writable)', 'not synced'
    log.heading(heading)
    for f in files:
        log.info(os.path.relpath(f, workspace_dir))
    log.warning(f'{plural(len(files), "file")} {outcome}')
