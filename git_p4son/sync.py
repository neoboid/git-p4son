"""
Sync command implementation for git-p4son.
"""

import argparse
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from typing import IO

from .common import RunError, run_with_output
from .config import get_depot_root
from .git import (
    add_all_files, commit, find_base_commits, get_blob_oids,
    get_dirty_files, get_file_at_commit, get_head_commit, get_tracked_files,
    is_file_tracked, merge_file,
)
from .hooks import run_hooks
from .log import log
from .perforce import (
    get_client_spec,
    get_latest_changelist,
    get_writable_files,
    is_binary_file_type,
    p4_fstat_file_info,
    p4_get_opened_files,
    p4_sync_preview,
    P4SyncOutputProcessor,
    P4SyncPreviewFile,
)


@dataclass
class LastSync:
    """Info about the most recent p4son sync commit."""
    changelist: int
    commit: str


def git_last_sync(workspace_dir: str) -> LastSync | None:
    """Get the changelist number and commit SHA of the most recent sync commit."""
    res = run_with_output(
        ['git', 'log', '-1', '--pretty=%H %s',
         '--grep=: p4 sync //'],
        cwd=workspace_dir)
    if len(res.stdout) == 0:
        return None

    line = res.stdout[0]
    # Format: "<commit_hash> <subject>"
    parts = line.split(' ', 1)
    if len(parts) != 2:
        return None

    commit_hash, subject = parts
    pattern = r"^(\d+|pergit|git-p4son): p4 sync //.+@(\d+)$"
    match = re.search(pattern, subject)
    if not match:
        return None

    return LastSync(changelist=int(match.group(2)), commit=commit_hash)


@dataclass
class ChangedFile:
    """A writable file flagged for post-sync merge. HEAD and baseline content
    captured during classification is staged to disk under a per-sync temp
    root so it doesn't have to be held in memory through to the merge step.
    A None path means git had no version of the file at that commit."""
    filepath: str
    base_commit: str | None
    ours_path: str | None
    base_path: str | None
    is_binary: bool = False
    added_both: bool = False


@dataclass
class _ChangedFileMeta:
    """A file the user modified since its baseline, identified by blob OID
    comparison without reading content. Staged to disk only after the binary
    verdict is known. added_both marks an add/add: p4 is adding a file at a
    path that already has locally committed content, so there is no common
    baseline and the merge runs against an empty base."""
    filepath: str
    base_commit: str | None
    added_both: bool = False


@dataclass
class WritableSyncFileSet:
    """Writable files found during sync preview, classified."""
    changed: list[ChangedFile] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)


def _stage_temp_content(temp_root: str, rel_path: str, suffix: str,
                        content: bytes) -> str:
    """Write content to a file under temp_root mirroring rel_path with the
    given suffix appended. Returns the full path."""
    rel_norm = rel_path.replace('\\', '/')
    temp_path = os.path.join(temp_root, rel_norm + suffix)
    os.makedirs(os.path.dirname(temp_path), exist_ok=True)
    with open(temp_path, 'wb') as f:
        f.write(content)
    return temp_path


def _to_crlf(content: bytes) -> bytes:
    """Convert content to CRLF line endings. Normalizes to LF first so blobs
    that already contain CRLF (or mixed endings) don't get doubled \\r."""
    return content.replace(b'\r\n', b'\n').replace(b'\n', b'\r\n')


def _stage_changed_file(meta: _ChangedFileMeta, pre_sync_head_commit: str,
                        workspace_dir: str, temp_root: str,
                        is_binary: bool, uses_crlf: bool) -> ChangedFile:
    """Read HEAD and baseline content for a changed file and stage it.

    Text content is converted to the workspace line ending before the single
    write: git blobs are LF, but a Perforce workspace with LineEnd win/local
    writes files to disk as CRLF, and without matching endings the post-sync
    merge sees every line as changed and conflicts the whole file. Binary
    files are staged verbatim; their ours blob is restored byte-for-byte."""
    rel_path = os.path.relpath(meta.filepath, workspace_dir)
    ours = get_file_at_commit(
        rel_path, pre_sync_head_commit, workspace_dir)
    base = None
    if meta.base_commit is not None:
        base = get_file_at_commit(
            rel_path, meta.base_commit, workspace_dir)

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


def prepare_writable_files(preview_files: list[P4SyncPreviewFile],
                           workspace_dir: str,
                           pre_sync_head_commit: str,
                           temp_root: str,
                           uses_crlf: bool = False,
                           clobber: bool = False) -> WritableSyncFileSet:
    """Check which preview files are writable on disk and prepare them for sync.

    For tracked writable files, queries Perforce for the file type (binary
    detection) and uses git to decide whether the user has modified the file
    since the last sync that touched it. Files that haven't been modified
    are just made read-only. Modified files are made read-only and added
    to the changed list for post-sync merging. Files p4 will add over local
    content (add/add) are always queued for merge against an empty base.

    clobber reflects the workspace clobber option. git-ignored writable files
    are left writable and rely on p4 refusing to overwrite them, which only
    holds when clobber is off; with clobber on p4 overwrites them, so the
    reported outcome differs.
    """
    result = WritableSyncFileSet()

    log.heading('Detecting writable files')
    writable = []
    added_upstream = set()
    for entry in preview_files:
        try:
            mode = os.stat(entry.filepath).st_mode
            if mode & stat.S_IWUSR:
                writable.append(entry.filepath)
                if entry.mode == 'add':
                    added_upstream.add(entry.filepath)
        except OSError:
            pass

    log.success(f'{len(writable)}/{len(preview_files)} are writable')
    if not writable:
        return result

    log.heading('Splitting writable files into tracked and ignored')
    # Tracking status is the discriminator, not ignore patterns: a tracked
    # file matching a .gitignore pattern (common when .gitignore was copied
    # from .p4ignore at init) must still sync, and an untracked writable
    # file is necessarily git-ignored since sync requires a clean workspace.
    tracked_set = get_tracked_files(writable, workspace_dir)
    tracked = [f for f in writable if f in tracked_set]
    result.ignored = [f for f in writable if f not in tracked_set]
    log.success(f'{len(tracked)} tracked, {len(result.ignored)} ignored')

    if not tracked:
        return result

    # Pass 1: decide which files the user modified since their baseline by
    # comparing git blob OIDs, transferring no content. Baselines and OIDs
    # for all files are resolved in batched git calls (one history walk plus
    # one cat-file); with hundreds of writable files after a branch switch,
    # per-file process spawning dominated the entire sync.
    log.heading('Detecting modified tracked writable files')
    for f in tracked:
        # Make read-only regardless of whether changed or not
        mode = os.stat(f).st_mode
        os.chmod(f, mode & ~stat.S_IWUSR)

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
            # p4 will *add* this file: its have-list says the client never
            # had it, so the local content cannot have come from Perforce.
            # The git baseline (the file's own introducing commit) says
            # nothing about p4 state here, so skip the blob comparison and
            # always merge, against an empty base.
            metas.append(_ChangedFileMeta(
                filepath=f, base_commit=None, added_both=True))
            continue

        # Since sync_command verifies the workspace is clean, "HEAD content"
        # also means "on-disk content". HEAD as the baseline, or a HEAD blob
        # identical to the baseline blob, means the user has not modified
        # the file since the baseline; no merge needed.
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

    # Pass 2: query Perforce for file type only on the changed subset. The
    # binary verdict must be known before staging so text content can be
    # written once in the workspace line ending (its only authoritative
    # source is p4's headType, and the merge step restores binary ours blobs
    # byte-for-byte). Pass 3: read content once, convert, stage.
    if metas:
        log.heading('Finding file types (text/binary)')
        file_info = p4_fstat_file_info(
            [m.filepath for m in metas], workspace_dir)
        log.success('')

        log.heading('Staging tracked changed files for post-sync merge')
        for m in metas:
            info = file_info.get(m.filepath)
            is_binary = bool(info and is_binary_file_type(info.head_type))
            result.changed.append(_stage_changed_file(
                m, pre_sync_head_commit, workspace_dir, temp_root,
                is_binary, uses_crlf))
        log.success('')

    # Log what we found
    log.heading('Prepare sync summary')
    if unchanged_count:
        label = 'file' if unchanged_count == 1 else 'files'
        log.success(
            f'{unchanged_count} writable {label} unchanged, '
            'skipping merge')

    if result.changed:
        count = len(result.changed)
        label = 'file has' if count == 1 else 'files have'
        log.warning(f'{count} {label} local changes, will merge after sync')
        for cf in result.changed:
            log.info(os.path.relpath(cf.filepath, workspace_dir))

    if result.ignored:
        count = len(result.ignored)
        label = 'file' if count == 1 else 'files'
        if clobber:
            # These files are left writable, so with clobber enabled p4
            # overwrites them during sync rather than refusing to.
            log.warning(
                f'{count} git-ignored writable {label} will be overwritten '
                'by p4 (clobber is enabled on the workspace)')
        else:
            log.warning(
                f'{count} git-ignored writable {label} will not be synced')
        for f in result.ignored:
            log.info(os.path.relpath(f, workspace_dir))

    return result


def _make_writable(filepath: str) -> None:
    """Add user write permission to a file if it is read-only."""
    mode = os.stat(filepath).st_mode
    if not mode & stat.S_IWUSR:
        os.chmod(filepath, mode | stat.S_IWUSR)


def _merge_changed_files(changed_files: list[ChangedFile],
                         workspace_dir: str,
                         temp_root: str) -> None:
    """Merge local changes back into the workspace after syncing.

    Each ChangedFile points at temp files staged during classification, so
    the merge step does no extra git queries and only reads file content
    when copying or comparing."""
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

        # Get the Perforce version (on disk after sync)
        theirs_exists = os.path.exists(filepath)

        # Handle add/delete asymmetry. If the file is gone upstream there is
        # nothing to merge against; let the delete stand. The local version
        # (if any) remains recoverable from git history. Any ChangedFile
        # whose ours_path is set is by construction one we couldn't prove
        # unchanged against the baseline, so we always flag it.
        if not theirs_exists:
            if cf.ours_path is not None:
                deleted_upstream_with_local_changes.append(filepath)
            continue

        if cf.ours_path is None:
            # Deleted locally, modified in Perforce
            deleted_local_added_upstream.append(filepath)
            continue

        # Check if binary using Perforce file type
        if cf.is_binary:
            # Binary file - restore user's version
            _make_writable(filepath)
            shutil.copyfile(cf.ours_path, filepath)
            binary_file_list.append(filepath)
            continue

        # Three-way merge using git merge-file directly on the staged paths.
        # Staged ours/base already carry the workspace line ending (handled in
        # prepare_writable_files), so no conversion is needed here.
        # When no baseline commit exists, fall back to a shared empty file.
        base_path = cf.base_path
        if base_path is None:
            if empty_base_path is None:
                empty_base_path = os.path.join(temp_root, '.empty_base')
                open(empty_base_path, 'wb').close()
            base_path = empty_base_path

        clean, merged = merge_file(filepath, base_path, cf.ours_path)
        _make_writable(filepath)
        with open(filepath, 'wb') as f:
            f.write(merged)

        if clean:
            merged_clean.append(filepath)
        elif cf.added_both:
            added_both_conflicts.append(filepath)
        else:
            merged_conflicts.append(filepath)

    # Report results
    if merged_clean:
        count = len(merged_clean)
        label = 'file' if count == 1 else 'files'
        log.success(f'{count} {label} merged successfully')
        for f in merged_clean:
            log.info(os.path.relpath(f, workspace_dir))

    if merged_conflicts:
        count = len(merged_conflicts)
        label = 'file' if count == 1 else 'files'
        log.warning(f'{count} {label} merged with conflicts')
        for f in merged_conflicts:
            log.info(os.path.relpath(f, workspace_dir))

    if added_both_conflicts:
        count = len(added_both_conflicts)
        label = 'file was' if count == 1 else 'files were'
        log.warning(
            f'{count} {label} added both locally and in Perforce - '
            'no common baseline, conflict markers show both full versions')
        for f in added_both_conflicts:
            log.info(os.path.relpath(f, workspace_dir))

    if binary_file_list:
        count = len(binary_file_list)
        label = 'binary file has' if count == 1 else 'binary files have'
        log.warning(f'{count} {label} local changes, local version restored')
        for f in binary_file_list:
            log.info(os.path.relpath(f, workspace_dir))

    if deleted_upstream_with_local_changes:
        count = len(deleted_upstream_with_local_changes)
        label = 'file' if count == 1 else 'files'
        log.warning(
            f'{count} {label} deleted in Perforce but modified locally, '
            'local edits available via git history')
        for f in deleted_upstream_with_local_changes:
            log.info(os.path.relpath(f, workspace_dir))

    if deleted_local_added_upstream:
        count = len(deleted_local_added_upstream)
        label = 'file' if count == 1 else 'files'
        log.warning(
            f'{count} {label} deleted locally but modified in Perforce')
        for f in deleted_local_added_upstream:
            log.info(os.path.relpath(f, workspace_dir))

    needs_attention = (merged_clean or merged_conflicts
                       or added_both_conflicts or binary_file_list
                       or deleted_upstream_with_local_changes
                       or deleted_local_added_upstream)
    if needs_attention:
        if merged_conflicts or added_both_conflicts:
            log.info('')
            log.info(
                'Manually review changes, resolve conflicts and commit when ready.')
        else:
            log.info('')
            log.info('Manually review changes and commit when ready.')


def p4_sync(changelist: int, label: str, depot_root: str,
            workspace_dir: str,
            expected_clobber: set[str] | None = None) -> None:
    """Sync files from Perforce.

    If expected_clobber is provided, clobber errors for those files are
    tolerated (they are git-ignored writable files). Unexpected clobber
    errors cause a raise.
    """
    log.heading(f'Syncing to {label} CL ({changelist})')

    output_processor = P4SyncOutputProcessor()
    try:
        result = run_with_output(
            ['p4', 'sync', f'{depot_root}/...@{changelist}'],
            cwd=workspace_dir, on_output=output_processor)
        if result.elapsed:
            log.elapsed(result.elapsed)
        log.success(output_processor.get_summary())
    except RunError as e:
        writable_files = get_writable_files(e.stderr)
        if not writable_files:
            raise

        # Check if all clobber errors are expected
        expected = expected_clobber or set()
        unexpected = [f for f in writable_files if f not in expected]
        if unexpected:
            log.error('Unexpected clobber errors:')
            for f in unexpected:
                log.info(f)
            raise

        log.info(output_processor.get_summary())
        log.warning(
            f'{len(writable_files)} expected clobber errors (git-ignored files)'
        )


def sync_command(args: argparse.Namespace) -> int:
    """Execute the sync command."""
    workspace_dir = args.workspace_dir
    invocation_dir = vars(args).get('invocation_dir', workspace_dir)

    log.heading('Finding depot root')
    depot_root = get_depot_root(workspace_dir)
    if not depot_root:
        log.error('No depot root configured. Run "git p4son init" first.')
        return 1
    log.success(depot_root)

    log.heading('Checking git workspace')
    dirty_files = get_dirty_files(workspace_dir)
    if dirty_files:
        for filename, change in dirty_files:
            log.file_change(filename, change)
        log.error('Workspace is not clean')
        return 1
    log.success('clean')

    log.heading('Checking p4 workspace')
    opened_files = p4_get_opened_files(depot_root, workspace_dir)
    tracked_opened_files = [
        (filename, change)
        for filename, change in opened_files
        if is_file_tracked(filename, workspace_dir)
    ]
    if tracked_opened_files:
        for filename, change in tracked_opened_files:
            log.file_change(filename, change)
        log.error('Workspace has p4-opened files tracked by git')
        return 1
    if opened_files:
        log.warning(
            f'Ignoring {len(opened_files)} p4-opened files not tracked by git')
    else:
        log.success('Clean')

    last_changelist_label = 'last synced'
    log.heading(f'Finding {last_changelist_label} changelist')
    last_sync = git_last_sync(workspace_dir)
    if last_sync:
        log.success(f'CL {last_sync.changelist}')
    else:
        log.warning('No previous sync found')

    log.heading('Finding HEAD commit')
    pre_sync_head_commit = get_head_commit(workspace_dir)
    log.success(f'{pre_sync_head_commit}')

    # Workspace line ending governs how staged git content is normalized so
    # the post-sync merge doesn't conflict on LF-vs-CRLF differences alone.
    # The clobber option changes whether git-ignored writable files survive
    # the sync, which the prepare summary needs to report accurately.
    client_spec = get_client_spec(workspace_dir)
    uses_crlf = bool(client_spec and client_spec.uses_crlf)
    clobber = bool(client_spec and client_spec.clobber)

    # Temp root for staging HEAD/baseline file content between classification
    # and the post-sync merge. Cleaned up automatically on exit.
    with tempfile.TemporaryDirectory(prefix='git-p4son-sync-') as temp_root:
        if args.changelist is not None \
                and args.changelist.lower() == 'last-synced':
            if not last_sync:
                log.error('No previous sync found, cannot use "last-synced"')
                return 1
            preview = p4_sync_preview(
                last_sync.changelist, depot_root, workspace_dir)
            if preview:
                prep = prepare_writable_files(preview, workspace_dir,
                                              pre_sync_head_commit, temp_root,
                                              uses_crlf=uses_crlf, clobber=clobber)
                p4_sync(last_sync.changelist, last_changelist_label,
                        depot_root, workspace_dir,
                        expected_clobber=set(prep.ignored))
            run_hooks('post-sync', workspace_dir, invocation_dir)
            return 0

        # No argument means sync to latest
        if args.changelist is None:
            log.heading('Finding latest changelist')
            changelist = get_latest_changelist(depot_root, workspace_dir)
            log.success(f'CL {changelist}')
            changelist_label = 'latest'
        else:
            changelist_label = 'specified'
            try:
                changelist = int(args.changelist)
            except ValueError:
                log.error(f'Invalid changelist number: {args.changelist}')
                return 1

        last_changelist = last_sync.changelist if last_sync else None
        if last_changelist == changelist:
            log.info(f'Already at CL {last_changelist}, nothing to do.')
            log.heading('Skipping post-sync hooks')
            return 0

        # Check if trying to sync to an older changelist
        if last_sync and changelist < last_sync.changelist:
            if not args.force:
                log.error(
                    f'Cannot sync to CL {changelist} '
                    f'(currently at CL {last_sync.changelist}) without --force.')
                return 1
            else:
                log.warning(
                    f'Syncing to older CL {changelist} '
                    f'(currently at CL {last_sync.changelist}) with --force')

        # First sync pass: to last_synced_cl
        all_changed: list[ChangedFile] = []
        all_ignored: list[str] = []

        if last_changelist is not None:
            preview = p4_sync_preview(
                last_changelist, depot_root, workspace_dir)
            prep = prepare_writable_files(preview, workspace_dir,
                                          pre_sync_head_commit, temp_root,
                                          uses_crlf=uses_crlf, clobber=clobber)
            all_changed.extend(prep.changed)
            all_ignored.extend(prep.ignored)
            if preview:
                p4_sync(last_changelist, last_changelist_label, depot_root,
                        workspace_dir, expected_clobber=set(prep.ignored))

        # Second sync pass: to target CL
        preview = p4_sync_preview(changelist, depot_root, workspace_dir)
        prep = prepare_writable_files(preview, workspace_dir,
                                      pre_sync_head_commit, temp_root,
                                      uses_crlf=uses_crlf, clobber=clobber)
        all_changed.extend(prep.changed)
        all_ignored.extend(prep.ignored)
        if preview:
            p4_sync(changelist, changelist_label, depot_root,
                    workspace_dir, expected_clobber=set(prep.ignored))

        # Commit (pure Perforce state)
        log.heading('Committing git changes')
        dirty_files = get_dirty_files(workspace_dir)
        if dirty_files:
            add_all_files(workspace_dir)

        commit_msg = f'git-p4son: p4 sync {depot_root}/...@{changelist}'
        commit(commit_msg, workspace_dir, allow_empty=True)
        log.success(f'Committed {len(dirty_files)} files')

        # Post-commit: merge changed files back. Dedup by filepath in case
        # the same file shows up in both sync passes.
        by_path: dict[str, ChangedFile] = {}
        for cf in all_changed:
            by_path[cf.filepath] = cf
        changed_files = sorted(by_path.values(), key=lambda cf: cf.filepath)
        _merge_changed_files(changed_files, workspace_dir, temp_root)

        # Report git-ignored writable files. Left writable, they are only
        # preserved because p4 refuses to clobber them; with clobber on p4
        # overwrites them during the sync instead.
        if all_ignored:
            if clobber:
                log.heading(
                    'Git-ignored writable files overwritten by p4 '
                    '(clobber enabled)')
            else:
                log.heading('Files not synced (git-ignored and writable)')
            for f in sorted(set(all_ignored)):
                log.info(os.path.relpath(f, workspace_dir))

        run_hooks('post-sync', workspace_dir, invocation_dir)
        return 0
