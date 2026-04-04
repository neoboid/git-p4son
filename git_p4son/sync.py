"""
Sync command implementation for git-p4son.
"""

import argparse
import os
import re
import stat
from dataclasses import dataclass, field
from typing import IO

from .common import RunError, compute_local_md5, run_with_output
from .config import get_depot_root
from .git import (
    add_all_files, commit, get_dirty_files, get_file_at_commit,
    get_head_commit, get_ignored_files, merge_file,
)
from .log import log
from .perforce import (
    get_latest_changelist,
    get_writable_files,
    is_binary_file_type,
    p4_fstat_file_info,
    p4_get_opened_files,
    p4_sync_preview,
    P4SyncOutputProcessor,
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
class WritableSyncFileSet:
    """Writable files found during sync preview, classified."""
    changed: list[str] = field(default_factory=list)
    ignored: list[str] = field(default_factory=list)
    binary: set[str] = field(default_factory=set)


def prepare_writable_files(preview_files: list[str],
                           workspace_dir: str) -> WritableSyncFileSet:
    """Check which preview files are writable on disk and prepare them for sync.

    For tracked writable files, queries Perforce for file type and MD5 digest.
    Files with matching MD5 are just made read-only (unchanged, no merge needed).
    Files with different MD5 are made read-only and added to the changed list
    for post-sync merging. Binary file info is also collected.
    """
    result = WritableSyncFileSet()

    # Find files that exist on disk and are writable
    writable = []
    for f in preview_files:
        try:
            mode = os.stat(f).st_mode
            if mode & stat.S_IWUSR:
                writable.append(f)
        except OSError:
            pass

    if not writable:
        return result

    # Split into ignored vs tracked
    ignored_set = get_ignored_files(writable, workspace_dir)
    result.ignored = [f for f in writable if f in ignored_set]
    tracked = [f for f in writable if f not in ignored_set]

    if not tracked:
        return result

    # Query Perforce for file type and MD5 digest
    file_info = p4_fstat_file_info(tracked, workspace_dir)

    # Classify tracked files by comparing local MD5 with Perforce digest
    unchanged_count = 0
    for f in tracked:
        # Make read-only regardless of whether changed or not
        mode = os.stat(f).st_mode
        os.chmod(f, mode & ~stat.S_IWUSR)

        info = file_info.get(f)
        if info and info.digest:
            local_md5 = compute_local_md5(f)
            if local_md5 == info.digest:
                unchanged_count += 1
                continue

        result.changed.append(f)

    # Collect binary file info
    result.binary = {f for f, info in file_info.items()
                     if is_binary_file_type(info.head_type)}

    # Log what we found
    if unchanged_count:
        label = 'file' if unchanged_count == 1 else 'files'
        log.success(
            f'{unchanged_count} writable {label} unchanged, '
            'skipping merge')

    if result.changed:
        count = len(result.changed)
        label = 'file has' if count == 1 else 'files have'
        log.warning(f'{count} {label} local changes, will merge after sync')
        for f in result.changed:
            log.info(f)

    if result.ignored:
        count = len(result.ignored)
        label = 'file' if count == 1 else 'files'
        log.warning(
            f'{count} git-ignored writable {label} will not be synced')
        for f in result.ignored:
            log.info(f)

    return result


def _make_writable(filepath: str) -> None:
    """Add user write permission to a file if it is read-only."""
    mode = os.stat(filepath).st_mode
    if not mode & stat.S_IWUSR:
        os.chmod(filepath, mode | stat.S_IWUSR)


def _merge_changed_files(changed_files: list[str], user_commit: str,
                         last_sync_commit: str | None,
                         workspace_dir: str,
                         binary_files: set[str] | None = None) -> None:
    """Merge local changes back into the workspace after syncing."""
    if not changed_files:
        return

    log.heading('Merging local changes')

    merged_clean = []
    merged_conflicts = []
    binary_file_list = []
    added_local_deleted_upstream = []
    deleted_local_added_upstream = []

    for filepath in changed_files:
        # Convert absolute paths to repo-relative for git operations
        rel_path = os.path.relpath(filepath, workspace_dir)

        # Get the user's version (before sync)
        ours = get_file_at_commit(rel_path, user_commit, workspace_dir)

        # Get the base version (from last sync commit)
        base = None
        if last_sync_commit:
            base = get_file_at_commit(
                rel_path, last_sync_commit, workspace_dir)

        # Get the Perforce version (on disk after sync)
        theirs_exists = os.path.exists(filepath)

        # Handle add/delete asymmetry
        if ours is not None and not theirs_exists:
            if ours == base:
                # File unchanged from last sync - user didn't modify it,
                # just the read-only flag was cleared by git. Let the
                # upstream delete stand.
                continue
            # Modified locally, deleted in Perforce - restore user's version
            with open(filepath, 'wb') as f:
                f.write(ours)
            added_local_deleted_upstream.append(filepath)
            continue

        if ours is None and theirs_exists:
            # Deleted locally, modified in Perforce
            deleted_local_added_upstream.append(filepath)
            continue

        if ours is None or not theirs_exists:
            # Both deleted - nothing to do
            continue

        # Check if binary using Perforce file type
        if binary_files and filepath in binary_files:
            # Binary file - restore user's version
            _make_writable(filepath)
            with open(filepath, 'wb') as f:
                f.write(ours)
            binary_file_list.append(filepath)
            continue

        # Read theirs from disk
        with open(filepath, 'rb') as f:
            theirs = f.read()

        # Three-way merge
        if base is None:
            base = b''

        clean, merged = merge_file(theirs, base, ours, rel_path)
        _make_writable(filepath)
        with open(filepath, 'wb') as f:
            f.write(merged)

        if clean:
            merged_clean.append(filepath)
        else:
            merged_conflicts.append(filepath)

    # Report results
    if merged_clean:
        count = len(merged_clean)
        label = 'file' if count == 1 else 'files'
        log.success(f'{count} {label} merged successfully')
        for f in merged_clean:
            log.info(f)

    if merged_conflicts:
        count = len(merged_conflicts)
        label = 'file' if count == 1 else 'files'
        log.warning(f'{count} {label} merged with conflicts')
        for f in merged_conflicts:
            log.info(f)

    if binary_file_list:
        count = len(binary_file_list)
        label = 'binary file has' if count == 1 else 'binary files have'
        log.warning(f'{count} {label} local changes, local version restored')
        for f in binary_file_list:
            log.info(f)

    if added_local_deleted_upstream:
        count = len(added_local_deleted_upstream)
        label = 'file' if count == 1 else 'files'
        log.warning(
            f'{count} {label} deleted in Perforce but modified locally, '
            'local version restored')
        for f in added_local_deleted_upstream:
            log.info(f)

    if deleted_local_added_upstream:
        count = len(deleted_local_added_upstream)
        label = 'file' if count == 1 else 'files'
        log.warning(
            f'{count} {label} deleted locally but modified in Perforce')
        for f in deleted_local_added_upstream:
            log.info(f)

    needs_attention = (merged_clean or merged_conflicts or binary_file_list
                       or added_local_deleted_upstream
                       or deleted_local_added_upstream)
    if needs_attention:
        if merged_conflicts:
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
    if opened_files:
        for filename, change in opened_files:
            log.file_change(filename, change)
        log.error('Workspace is not clean')
        return 1
    log.success('Clean')

    last_changelist_label = 'last synced'
    log.heading(f'Finding {last_changelist_label} changelist')
    last_sync = git_last_sync(workspace_dir)
    if last_sync:
        log.success(f'CL {last_sync.changelist}')
    else:
        log.warning('No previous sync found')

    if args.changelist is not None and args.changelist.lower() == 'last-synced':
        if not last_sync:
            log.error('No previous sync found, cannot use "last-synced"')
            return 1
        p4_sync(last_sync.changelist, last_changelist_label, depot_root,
                workspace_dir)
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

    # Remember current state for merging later
    log.heading('Finding HEAD commit')
    user_commit = get_head_commit(workspace_dir)
    log.success(f'{user_commit}')

    last_sync_commit = last_sync.commit if last_sync else None

    # First sync pass: to last_synced_cl
    all_changed: list[str] = []
    all_ignored: list[str] = []
    all_binary: set[str] = set()

    if last_changelist is not None:
        preview = p4_sync_preview(
            last_changelist, depot_root, workspace_dir)
        prep = prepare_writable_files(preview, workspace_dir)
        all_changed.extend(prep.changed)
        all_ignored.extend(prep.ignored)
        all_binary |= prep.binary
        p4_sync(last_changelist, last_changelist_label, depot_root,
                workspace_dir, expected_clobber=set(prep.ignored))

    # Second sync pass: to target CL
    preview = p4_sync_preview(changelist, depot_root, workspace_dir)
    prep = prepare_writable_files(preview, workspace_dir)
    all_changed.extend(prep.changed)
    all_ignored.extend(prep.ignored)
    all_binary |= prep.binary
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

    # Post-commit: merge changed files back
    changed_files = sorted(set(all_changed))
    _merge_changed_files(changed_files, user_commit,
                         last_sync_commit, workspace_dir,
                         binary_files=all_binary)

    # Report git-ignored files that could not be synced
    if all_ignored:
        log.heading('Files not synced (git-ignored and writable)')
        for f in sorted(set(all_ignored)):
            log.info(f)

    return 0
