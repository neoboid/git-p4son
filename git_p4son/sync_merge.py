"""Saving local changes to the files a sync overwrites, and merging them back afterwards."""

import os
import shutil
from dataclasses import dataclass, field

from .git import (
    find_base_commits, get_blob_oids, get_files_at_commits, get_tracked_files,
    merge_file,
)
from .log import log
from .perforce import (
    ClobberMode,
    P4SyncPreviewFile,
    is_always_writable_file_type,
    is_binary_file_type,
    p4_head_types,
)
from .writable import is_user_writable, make_read_only, make_writable

_MERGE_LABELS = ('Perforce', 'base', 'local')


@dataclass
class ChangedFile:
    """A writable file to merge after sync; a None staged path means git had no version."""
    filepath: str
    base_commit: str | None
    ours_path: str | None
    base_path: str | None
    is_binary: bool = False
    added_both: bool = False


@dataclass
class _ChangedFileMeta:
    """A file modified since its baseline; added_both means p4 adds it over local content."""
    filepath: str
    base_commit: str | None
    added_both: bool = False


@dataclass
class WritableSyncFileSet:
    """Writable files found during sync preview, classified."""
    changed: list[ChangedFile] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    always_writable: list[str] = field(default_factory=list)
    # Ignored files p4 refused to overwrite, known only after the sync.
    not_synced: list[str] = field(default_factory=list)
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


def _stage_changed_file(meta: _ChangedFileMeta, rel_path: str,
                        ours: bytes | None, base: bytes | None,
                        temp_root: str, is_binary: bool,
                        uses_crlf: bool) -> ChangedFile:
    """Stage HEAD and baseline content of a changed file, text in the workspace line ending."""
    if uses_crlf and not is_binary:
        if ours is not None:
            ours = _to_crlf(ours)
        if base is not None:
            base = _to_crlf(base)

    ours_path = (_stage_temp_content(temp_root, rel_path, '.ours', ours)
                 if ours is not None else None)
    base_path = (_stage_temp_content(temp_root, rel_path, '.base', base)
                 if base is not None else None)
    return ChangedFile(filepath=meta.filepath, base_commit=meta.base_commit,
                       ours_path=ours_path, base_path=base_path,
                       is_binary=is_binary, added_both=meta.added_both)


def _log_file_group(log_fn, files: list[str], singular: str, plural: str,
                    text: str, workspace_dir: str | None = None) -> None:
    """Log how many files are in a group, then list them if workspace_dir is given."""
    if not files:
        return
    label = singular if len(files) == 1 else plural
    log_fn(f'{len(files)} {label} {text}')
    if workspace_dir is not None:
        for f in files:
            log.info(os.path.relpath(f, workspace_dir))


def _log_prepare_summary(result: WritableSyncFileSet, workspace_dir: str,
                         clobber_mode: ClobberMode,
                         unchanged_count: int = 0) -> None:
    """Log what the writable-file classification found."""
    log.heading('Prepare sync summary')
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

    singular, plural = 'git-ignored writable file', 'git-ignored writable files'
    if clobber_mode is ClobberMode.CLOBBER:
        _log_file_group(log.warning, result.ignored, singular, plural,
                        'will be overwritten by p4 (clobber is enabled on the workspace)',
                        workspace_dir)
    elif clobber_mode is ClobberMode.ALLWRITE:
        # The skipped ones are only known after the sync, which lists them.
        _log_file_group(log.info, result.ignored, singular, plural,
                        'will be synced unless modified locally')
    else:
        _log_file_group(log.warning, result.ignored, singular, plural,
                        'will not be synced', workspace_dir)


def prepare_writable_files(preview_files: list[P4SyncPreviewFile],
                           workspace_dir: str,
                           pre_sync_head_commit: str,
                           temp_root: str,
                           uses_crlf: bool,
                           clobber_mode: ClobberMode
                           ) -> WritableSyncFileSet:
    """Classify the preview's writable files and stage the modified ones for post-sync merge."""
    result = WritableSyncFileSet()

    log.heading('Detecting writable files')
    writable = []
    added_upstream = set()
    for entry in preview_files:
        if is_user_writable(entry.filepath):
            writable.append(entry.filepath)
            if entry.mode == 'add':
                added_upstream.add(entry.filepath)

    log.success(f'{len(writable)}/{len(preview_files)} are writable')
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
            if head_type and is_always_writable_file_type(head_type):
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
        if base is not None and base != pre_sync_head_commit:
            oid_queries.append((pre_sync_head_commit, rel_paths[f]))
            oid_queries.append((base, rel_paths[f]))
    oids = get_blob_oids(oid_queries, workspace_dir)

    unchanged_count = 0
    metas: list[_ChangedFileMeta] = []
    for f in tracked:
        if f in added_upstream:
            # p4 adds a file the client never had: always merge, against an empty base.
            metas.append(_ChangedFileMeta(
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

        metas.append(_ChangedFileMeta(filepath=f, base_commit=base))

    log.success(f'{len(metas)} changed, {unchanged_count} unchanged')

    if clobber_mode is ClobberMode.ALLWRITE:
        # p4 overwrites unmodified files cleanly; only strip the ones it would refuse.
        make_read_only([m.filepath for m in metas])

    # Pass 2: file types of the changed files. Pass 3: stage their content.
    if metas:
        log.heading('Finding file types (text/binary)')
        head_types = p4_head_types(
            [m.filepath for m in metas], workspace_dir)
        log.success('')

        log.heading('Staging tracked changed files for post-sync merge')
        queries = []
        for m in metas:
            queries.append((pre_sync_head_commit, rel_paths[m.filepath]))
            if m.base_commit is not None:
                queries.append((m.base_commit, rel_paths[m.filepath]))
        contents = get_files_at_commits(queries, workspace_dir)
        for m in metas:
            rel = rel_paths[m.filepath]
            head_type = head_types.get(m.filepath)
            is_binary = bool(head_type and is_binary_file_type(head_type))
            result.changed.append(_stage_changed_file(
                m, rel, contents[(pre_sync_head_commit, rel)],
                contents.get((m.base_commit, rel)), temp_root, is_binary,
                uses_crlf))
        log.success('')

    _log_prepare_summary(result, workspace_dir, clobber_mode, unchanged_count)
    return result


def merge_changed_files(changed_files: list[ChangedFile],
                        workspace_dir: str,
                        temp_root: str) -> None:
    """Merge local changes back into the workspace after syncing."""
    if not changed_files:
        return

    log.heading('Merging local changes')

    merged_clean = []
    merged_conflicts = []
    added_both_conflicts = []
    binary_file_list = []
    deleted_upstream_with_local_changes = []
    deleted_local_added_upstream = []

    # Shared empty file used as base when no baseline commit exists.
    empty_base_path: str | None = None

    for cf in changed_files:
        filepath = cf.filepath
        rel_path = os.path.relpath(filepath, workspace_dir)
        log.info(f'{rel_path}: base = {cf.base_commit or "(none)"}')

        # Deleted upstream: let the delete stand, local edits stay in git history.
        if not os.path.exists(filepath):
            if cf.ours_path is not None:
                deleted_upstream_with_local_changes.append(filepath)
            continue

        if cf.ours_path is None:
            # Deleted locally, modified in Perforce
            deleted_local_added_upstream.append(filepath)
            continue

        if cf.is_binary:
            make_writable([filepath])
            shutil.copyfile(cf.ours_path, filepath)
            binary_file_list.append(filepath)
            continue

        base_path = cf.base_path
        if base_path is None:
            if empty_base_path is None:
                empty_base_path = os.path.join(temp_root, '.empty_base')
                open(empty_base_path, 'wb').close()
            base_path = empty_base_path

        clean, merged = merge_file(filepath, base_path, cf.ours_path,
                                   _MERGE_LABELS)
        make_writable([filepath])
        with open(filepath, 'wb') as f:
            f.write(merged)

        if clean:
            merged_clean.append(filepath)
        elif cf.added_both:
            added_both_conflicts.append(filepath)
        else:
            merged_conflicts.append(filepath)

    groups = [
        (log.success, merged_clean, 'file', 'files', 'merged successfully'),
        (log.warning, merged_conflicts, 'file', 'files', 'merged with conflicts'),
        (log.warning, added_both_conflicts, 'file was', 'files were',
         'added both locally and in Perforce - no common baseline, conflict markers show both full versions'),
        (log.warning, binary_file_list, 'binary file has', 'binary files have',
         'local changes, local version restored'),
        (log.warning, deleted_upstream_with_local_changes, 'file', 'files',
         'deleted in Perforce but modified locally, local edits available via git history'),
        (log.warning, deleted_local_added_upstream, 'file', 'files',
         'deleted locally but modified in Perforce'),
    ]
    for log_fn, files, singular, plural, text in groups:
        _log_file_group(log_fn, files, singular, plural, text, workspace_dir)

    if not any(files for _, files, *_ in groups):
        return
    log.info('')
    if merged_conflicts or added_both_conflicts:
        log.info('Manually review changes, resolve conflicts and commit when ready.')
    else:
        log.info('Manually review changes and commit when ready.')


def report_not_synced(preps: list[WritableSyncFileSet],
                      clobber_mode: ClobberMode, workspace_dir: str) -> None:
    """Log the git-ignored writable files p4 refused or overwrote."""
    if clobber_mode is ClobberMode.CLOBBER:
        reported = [f for p in preps for f in p.ignored]
        heading = 'Git-ignored writable files overwritten by p4 (clobber enabled)'
    elif clobber_mode is ClobberMode.ALLWRITE:
        reported = [f for p in preps for f in p.not_synced]
        heading = 'Files not synced (git-ignored and locally modified)'
    else:
        reported = [f for p in preps for f in p.ignored]
        heading = 'Files not synced (git-ignored and writable)'
    if reported:
        log.heading(heading)
        for f in sorted(set(reported)):
            log.info(os.path.relpath(f, workspace_dir))
