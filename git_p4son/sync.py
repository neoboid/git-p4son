"""Sync command implementation for git-p4son."""

import argparse
import os
import re
import shutil
import stat
import sys
import tempfile
from dataclasses import dataclass, field
from typing import IO

from .common import RunError, prompt_choice, run_with_output
from .state import dismiss_clobber_warning, is_clobber_warning_dismissed
from .git import (
    add_all_files, commit, find_base_commits, get_blob_oids,
    get_dirty_files, get_file_at_commit, get_head_commit, get_tracked_files,
    is_file_tracked, merge_file,
)
from .hooks import run_hooks
from .lib import check_git_workspace_clean
from .depot import resolve_depot_root
from .log import log
from .processes import check_no_blocking_processes
from .writable import is_writable_mode, make_writable
from .sync_split_users import (
    USER_PLACEHOLDER,
    check_p4_users,
    get_split_users,
    resolve_split_users,
)
from .perforce import (
    get_latest_changelist,
    get_submitted_changes,
    get_writable_files,
    is_always_writable_file_type,
    is_binary_file_type,
    p4_fstat_file_info,
    p4_get_opened_files,
    p4_sync_preview,
    P4Change,
    P4SyncOutputProcessor,
    P4SyncPreviewFile,
)


LAST_SYNCED_LABEL = 'last synced'
SPLIT_LABEL = 'split'


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


def _stage_changed_file(meta: _ChangedFileMeta, pre_sync_head_commit: str,
                        workspace_dir: str, temp_root: str,
                        is_binary: bool, uses_crlf: bool) -> ChangedFile:
    """Stage HEAD and baseline content of a changed file, text in the workspace line ending."""
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


def _log_prepare_summary(result: WritableSyncFileSet, workspace_dir: str,
                         clobber: bool, unchanged_count: int = 0,
                         allwrite: bool = False) -> None:
    """Log what the writable-file classification found."""
    log.heading('Prepare sync summary')
    if unchanged_count:
        label = 'file' if unchanged_count == 1 else 'files'
        log.success(
            f'{unchanged_count} writable {label} unchanged, '
            'skipping merge')

    if result.always_writable:
        count = len(result.always_writable)
        label = 'file is' if count == 1 else 'files are'
        log.success(
            f'{count} git-ignored {label} always writable (+w), '
            'p4 syncs them normally')

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
            log.warning(
                f'{count} git-ignored writable {label} will be overwritten '
                'by p4 (clobber is enabled on the workspace)')
        elif allwrite:
            # The skipped ones are only known after the sync, which lists them.
            log.info(
                f'{count} git-ignored writable {label} will be synced '
                'unless modified locally')
            return
        else:
            log.warning(
                f'{count} git-ignored writable {label} will not be synced')
        for f in result.ignored:
            log.info(os.path.relpath(f, workspace_dir))


def prepare_writable_files(preview_files: list[P4SyncPreviewFile],
                           workspace_dir: str,
                           pre_sync_head_commit: str,
                           temp_root: str,
                           uses_crlf: bool = False,
                           clobber: bool = False,
                           allwrite: bool = False) -> WritableSyncFileSet:
    """Classify the preview's writable files and stage the modified ones for post-sync merge."""
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
    # Split on tracking, not ignore patterns: a tracked file matching .gitignore must still sync.
    tracked_set = get_tracked_files(writable, workspace_dir)
    tracked = [f for f in writable if f in tracked_set]
    result.ignored = [f for f in writable if f not in tracked_set]
    log.success(f'{len(tracked)} tracked, {len(result.ignored)} ignored')

    if result.ignored:
        log.heading('Checking ignored files for the +w (always writable) type')
        ignored_info = p4_fstat_file_info(result.ignored, workspace_dir)
        always_writable = set()
        for f in result.ignored:
            info = ignored_info.get(f)
            if info and is_always_writable_file_type(info.head_type):
                always_writable.add(f)
        result.always_writable = [f for f in result.ignored
                                  if f in always_writable]
        result.ignored = [f for f in result.ignored
                          if f not in always_writable]
        log.success(f'{len(result.always_writable)} always writable (+w)')

    if not tracked:
        _log_prepare_summary(result, workspace_dir, clobber,
                             allwrite=allwrite)
        return result

    # Pass 1: find modified files by comparing blob OIDs, in batched git calls.
    log.heading('Detecting modified tracked writable files')
    if not allwrite:
        # noclobber refuses any writable file; the merge restores the modified ones.
        _clear_write_bits(tracked)

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

    if allwrite:
        # p4 overwrites unmodified files cleanly; only strip the ones it would refuse.
        _clear_write_bits([m.filepath for m in metas])

    # Pass 2: file types of the changed files. Pass 3: stage their content.
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

    _log_prepare_summary(result, workspace_dir, clobber, unchanged_count,
                         allwrite)
    return result


def _clear_write_bits(filepaths: list[str]) -> None:
    """Remove user write permission so p4 is willing to overwrite the files."""
    for filepath in filepaths:
        mode = os.stat(filepath).st_mode
        os.chmod(filepath, mode & ~stat.S_IWUSR)


def _make_writable(filepath: str) -> None:
    """Add user write permission to a file if it is read-only."""
    mode = os.stat(filepath).st_mode
    if not mode & stat.S_IWUSR:
        os.chmod(filepath, mode | stat.S_IWUSR)


def _merge_changed_files(changed_files: list[ChangedFile],
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

        theirs_exists = os.path.exists(filepath)

        # Deleted upstream: let the delete stand, local edits stay in git history.
        if not theirs_exists:
            if cf.ours_path is not None:
                deleted_upstream_with_local_changes.append(filepath)
            continue

        if cf.ours_path is None:
            # Deleted locally, modified in Perforce
            deleted_local_added_upstream.append(filepath)
            continue

        if cf.is_binary:
            _make_writable(filepath)
            shutil.copyfile(cf.ours_path, filepath)
            binary_file_list.append(filepath)
            continue

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
            expected_clobber: set[str] | None = None) -> list[str]:
    """Sync files from Perforce, returning the expected_clobber files p4 refused.

    Any other clobber error raises.
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
        return []
    except RunError as e:
        writable_files = get_writable_files(e.stderr)
        if not writable_files:
            raise

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
        return writable_files


def _handle_clobber_warning(clobber: bool, workspace_dir: str) -> bool:
    """Warn that clobber is no longer needed; return False if the user aborts."""
    if not clobber or is_clobber_warning_dismissed(workspace_dir):
        return True
    if not sys.stdin.isatty():
        return True

    log.heading('Clobber option enabled')
    log.warning(
        'Clobber is enabled on your workspace but git-p4son no longer needs '
        'it. You can safely disable it in perforce.')
    choice = prompt_choice('How to proceed?', ['continue', 'abort'])
    if choice == 'abort':
        log.info('Aborting')
        return False
    if choice == 'continue':
        dismiss_clobber_warning(workspace_dir)
        log.success('Will not warn about clobber again')
    return True


def _check_p4_workspace_clean(depot_root: str, workspace_dir: str) -> bool:
    """Report whether the p4 workspace has no git-tracked files opened."""
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
        return False
    if opened_files:
        log.warning(
            f'Ignoring {len(opened_files)} p4-opened files not tracked by git')
    else:
        log.success('Clean')
    return True


def _run_pre_sync_hooks(workspace_dir: str, invocation_dir: str) -> bool:
    """Run all pre-sync hooks; return False if any of them failed."""
    results = run_hooks('pre-sync', workspace_dir, invocation_dir)
    if any(result.returncode != 0 for result in results):
        log.error('Aborting sync because a pre-sync hook failed')
        return False
    return True


def sync_preflight(depot_root: str, workspace_dir: str, invocation_dir: str,
                   ignore_blocking_processes: bool = False) -> bool:
    """Gate a sync on blocking processes, clean workspaces and pre-sync hooks."""
    if not check_no_blocking_processes(workspace_dir,
                                       ignore_blocking_processes):
        return False
    if not check_git_workspace_clean(workspace_dir):
        return False
    if not _check_p4_workspace_clean(depot_root, workspace_dir):
        return False
    return _run_pre_sync_hooks(workspace_dir, invocation_dir)


def _sync_pass(changelist: int, label: str, depot_root: str,
               workspace_dir: str, pre_sync_head_commit: str, temp_root: str,
               uses_crlf: bool, clobber: bool,
               allwrite: bool) -> WritableSyncFileSet:
    """Run one sync pass: preview, prepare writable files, and p4 sync."""
    preview = p4_sync_preview(changelist, depot_root, workspace_dir)
    prep = prepare_writable_files(preview, workspace_dir, pre_sync_head_commit,
                                  temp_root, uses_crlf=uses_crlf,
                                  clobber=clobber, allwrite=allwrite)
    if preview:
        prep.not_synced = p4_sync(changelist, label, depot_root,
                                  workspace_dir,
                                  expected_clobber=set(prep.ignored))
        prep.synced = [entry.filepath for entry in preview]
    return prep


def _restore_writable(synced: list[str], workspace_dir: str) -> None:
    """In writable mode, make the git-tracked files this sync wrote writable."""
    if not synced or not is_writable_mode(workspace_dir):
        return
    log.heading('Making synced tracked files writable (writable mode)')
    tracked = sorted(get_tracked_files(sorted(set(synced)), workspace_dir))
    changed = make_writable(tracked)
    log.success(f'{changed} of {len(tracked)} tracked files made writable')


def build_sync_targets(changes: list[P4Change], users: list[str],
                       last_synced: int, required: list[int]) -> list[int]:
    """Build the sync sequence that splits out the given users' changelists.

    Each of their changelists is preceded by the one submitted just before it,
    so it lands in a commit of its own. changes is oldest first.
    """
    split: list[int] = []
    lowered = {u.lower() for u in users}
    for i, change in enumerate(changes):
        if change.change <= last_synced or change.user.lower() not in lowered:
            continue
        last = split[-1] if split else last_synced
        if i > 0 and changes[i - 1].change > last:
            split.append(changes[i - 1].change)
            last = split[-1]
        if change.change > last:
            split.append(change.change)
    return sorted(set(split).union(required))


def _check_split_user_args(names: list[str],
                           workspace_dir: str) -> list[str] | None:
    """Return the -u users as the server spells them, or None if any is unknown."""
    real = [name for name in names if name != USER_PLACEHOLDER]
    if not real:
        return list(names)
    checked = check_p4_users(real, workspace_dir)
    if checked is None:
        return None
    return [checked.get(name, name) for name in names]


def _split_targets(targets: list[tuple[int, str]], users: list[str],
                   last_synced: int, depot_root: str,
                   workspace_dir: str) -> list[tuple[int, str]]:
    """Add the targets that give each of users' changelists its own commit."""
    upper = targets[-1][0]
    log.heading(f'Finding changelists submitted to {depot_root} '
                f'in CL {last_synced}..{upper}')
    changes = get_submitted_changes(depot_root, last_synced, upper,
                                    workspace_dir)
    log.success(f'{len(changes)} changelists')

    lowered = {u.lower() for u in users}
    matched = [c for c in changes
               if c.change > last_synced and c.user.lower() in lowered]
    log.heading('Finding changelists to split into their own commits')
    if matched:
        for change in matched:
            log.info(f'CL {change.change} ({change.user})')
        label = 'changelist' if len(matched) == 1 else 'changelists'
        log.success(f'{len(matched)} {label} to split out')
    else:
        log.success('None found')

    labels = dict(targets)
    return [(cl, labels.get(cl, SPLIT_LABEL))
            for cl in build_sync_targets(changes, users, last_synced,
                                         list(labels))]


def _latest_target(depot_root: str, workspace_dir: str) -> tuple[int, str]:
    """Look up the latest submitted changelist as a sync target."""
    log.heading('Finding latest changelist')
    latest = get_latest_changelist(depot_root, workspace_dir)
    log.success(f'CL {latest}')
    return (latest, 'latest')


def _resolve_sync_targets(
        raw: list[str], last_sync: LastSync | None, depot_root: str,
        workspace_dir: str, force: bool) -> list[tuple[int, str]] | None:
    """Resolve the ordered (changelist, label) targets a sync will visit.

    Returns None for invalid arguments, and [] when already at the only target.
    """
    lowered = [c.lower() for c in raw]

    if 'head' in lowered and lowered.index('head') != len(lowered) - 1:
        log.error('The "head" keyword must come last')
        return None

    if not raw:
        targets = [_latest_target(depot_root, workspace_dir)]
    else:
        targets = []
        for c in raw:
            if c.lower() == 'head':
                targets.append(_latest_target(depot_root, workspace_dir))
            else:
                try:
                    targets.append((int(c), 'specified'))
                except ValueError:
                    log.error(f'Invalid changelist number: {c}')
                    return None

    numbers = [cl for cl, _ in targets]
    for prev, curr in zip(numbers, numbers[1:]):
        if curr <= prev:
            log.error('Changelists must be strictly increasing, '
                      f'got {prev} then {curr}')
            return None

    if last_sync and numbers[0] < last_sync.changelist:
        if not force:
            log.error(
                f'Cannot sync to CL {numbers[0]} '
                f'(currently at CL {last_sync.changelist}) without --force.')
            return None
        log.warning(
            f'Syncing to older CL {numbers[0]} '
            f'(currently at CL {last_sync.changelist}) with --force')

    if last_sync and last_sync.changelist in numbers:
        log.info(f'Skipping CL {last_sync.changelist} (already synced)')
        targets = [(cl, lbl) for cl, lbl in targets
                   if cl != last_sync.changelist]

    return targets


def sync_command(args: argparse.Namespace) -> int:
    """Execute the sync command."""
    workspace_dir = args.workspace_dir
    invocation_dir = vars(args).get('invocation_dir', workspace_dir)
    ignore_blocking_processes = vars(args).get('ignore_blocking_processes',
                                               False)

    resolved = resolve_depot_root(workspace_dir)
    if resolved is None:
        return 1
    depot_root = resolved.depot_root
    client_spec = resolved.client_spec

    log.heading(f'Finding {LAST_SYNCED_LABEL} changelist')
    last_sync = git_last_sync(workspace_dir)
    if last_sync:
        log.success(f'CL {last_sync.changelist}')
    else:
        log.warning('No previous sync found')

    extra_split_users = vars(args).get('split_user') or []
    if extra_split_users:
        extra_split_users = _check_split_user_args(extra_split_users,
                                                   workspace_dir)
        if extra_split_users is None:
            return 1
    configured_split_users = ([] if vars(args).get('no_split', False)
                              else get_split_users(workspace_dir))
    split_users = configured_split_users + extra_split_users

    lowered = [c.lower() for c in args.changelist]

    # "last-synced" re-syncs the current changelist and takes its own path below.
    resync_last_synced = 'last-synced' in lowered
    targets: list[tuple[int, str]] = []
    if resync_last_synced:
        if lowered != ['last-synced']:
            log.error('The "last-synced" keyword cannot be combined with '
                      'other changelists')
            return 1
        if not last_sync:
            log.error('No previous sync found, cannot use "last-synced"')
            return 1
    else:
        resolved_targets = _resolve_sync_targets(
            args.changelist, last_sync, depot_root, workspace_dir, args.force)
        if resolved_targets is None:
            return 1
        if not resolved_targets:
            log.info('Already synced, nothing to do.')
            log.heading('Skipping post-sync hooks')
            return 0
        targets = resolved_targets

    # Splitting needs a previous sync to look forward from.
    split = False
    if split_users and not resync_last_synced:
        if not last_sync:
            log.info('Not splitting out changelists: '
                     'no previous sync to start from')
        elif targets[0][0] < last_sync.changelist:
            log.info('Not splitting out changelists: '
                     'syncing to an older changelist')
        else:
            split = True

    dry_run = vars(args).get('dry_run', False)

    uses_crlf = bool(client_spec and client_spec.uses_crlf)
    clobber = bool(client_spec and client_spec.clobber)
    allwrite = bool(client_spec and client_spec.allwrite)

    # A dry run syncs nothing, so it skips the prompt, the checks and the hooks.
    if not dry_run:
        if not _handle_clobber_warning(clobber, workspace_dir):
            return 1

        # Before splitting, whose queries are the costly part.
        if not sync_preflight(depot_root, workspace_dir, invocation_dir,
                              ignore_blocking_processes):
            return 1

    if split:
        log.heading('Finding split users')
        users = resolve_split_users(split_users, workspace_dir)
        if users is None:
            return 1
        log.success(', '.join(users))
        targets = _split_targets(targets, users, last_sync.changelist,
                                 depot_root, workspace_dir)

    if split or dry_run:
        log.heading('Sync sequence')
        if resync_last_synced:
            log.success(f'{last_sync.changelist} ({LAST_SYNCED_LABEL})')
        else:
            log.success(' '.join(str(cl) for cl, _ in targets))
    if dry_run:
        log.info('Dry run, nothing synced.')
        return 0

    log.heading('Finding HEAD commit')
    pre_sync_head_commit = get_head_commit(workspace_dir)
    log.success(f'{pre_sync_head_commit}')

    with tempfile.TemporaryDirectory(prefix='git-p4son-sync-') as temp_root:
        if resync_last_synced:
            prep = _sync_pass(last_sync.changelist, LAST_SYNCED_LABEL,
                              depot_root, workspace_dir, pre_sync_head_commit,
                              temp_root, uses_crlf, clobber, allwrite)
            _restore_writable(prep.synced, workspace_dir)
            run_hooks('post-sync', workspace_dir, invocation_dir)
            return 0

        all_changed: list[ChangedFile] = []
        all_ignored: list[str] = []
        all_not_synced: list[str] = []
        all_synced: list[str] = []
        last_changelist = last_sync.changelist if last_sync else None

        # Catch-up pass to the last synced changelist, folded into the first commit.
        if last_changelist is not None:
            prep = _sync_pass(last_changelist, LAST_SYNCED_LABEL,
                              depot_root, workspace_dir, pre_sync_head_commit,
                              temp_root, uses_crlf, clobber, allwrite)
            all_changed.extend(prep.changed)
            all_ignored.extend(prep.ignored)
            all_not_synced.extend(prep.not_synced)
            all_synced.extend(prep.synced)

        # Local changes are merged back once at the end, so each commit is pure Perforce state.
        for changelist, changelist_label in targets:
            prep = _sync_pass(changelist, changelist_label, depot_root,
                              workspace_dir, pre_sync_head_commit, temp_root,
                              uses_crlf, clobber, allwrite)
            all_changed.extend(prep.changed)
            all_ignored.extend(prep.ignored)
            all_not_synced.extend(prep.not_synced)
            all_synced.extend(prep.synced)

            log.heading(f'Committing git changes for CL {changelist}')
            dirty_files = get_dirty_files(workspace_dir)
            if dirty_files:
                add_all_files(workspace_dir)
            commit_msg = f'git-p4son: p4 sync {depot_root}/...@{changelist}'
            commit(commit_msg, workspace_dir, allow_empty=True)
            log.success(f'Committed {len(dirty_files)} files')

        # Dedup files that showed up in several sync passes.
        by_path: dict[str, ChangedFile] = {}
        for cf in all_changed:
            by_path[cf.filepath] = cf
        changed_files = sorted(by_path.values(), key=lambda cf: cf.filepath)
        _merge_changed_files(changed_files, workspace_dir, temp_root)

        if clobber:
            reported = all_ignored
            heading = ('Git-ignored writable files overwritten by p4 '
                       '(clobber enabled)')
        elif allwrite:
            reported = all_not_synced
            heading = 'Files not synced (git-ignored and locally modified)'
        else:
            reported = all_ignored
            heading = 'Files not synced (git-ignored and writable)'
        if reported:
            log.heading(heading)
            for f in sorted(set(reported)):
                log.info(os.path.relpath(f, workspace_dir))

        _restore_writable(all_synced, workspace_dir)

        run_hooks('post-sync', workspace_dir, invocation_dir)
        return 0
