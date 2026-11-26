"""Resuming an interrupted sync from its sync progress, or giving it up."""

import argparse
import contextlib
import dataclasses
import os
import tempfile
from dataclasses import dataclass, field

from .backend import Backend
from .common import CommandError, RevisionNotFoundError
from .git import (
    SyncState, files_changed_outside_syncs, commit_files, delete_ref, is_sync_subject, get_files_at_commits,
    get_blob_oids, join_commits, resolve_commit, restore_files, set_ref, sync_state,
)
from .log import log, plural
from .processes import check_no_blocking_processes
from .sync_progress import (
    GIVE_UP, INTERSYNC_COMMITS_REF, INTERSYNC_REF, AboveCommit, IntersyncFile, IntersyncSave, PassLists,
    ProgressPass, ProgressRecorder, SyncProgress, local_path, local_paths, StaleSyncProgress, remove_sync_progress,
    workspace_path, write_sync_progress,
)
from .sync_merge import IntersyncChange, WritableSyncFileSet
from .writable import is_user_writable, is_writable_mode, make_read_only, make_writable
from .perforce import ClobberMode, p4_off_have, p4_print_start, p4_sync_pending, p4_sync_to_have, is_p4_temp_file
from .sync_passes import (
    catch_up_passes, check_p4_workspace_clean, end_sync, finish_on_branch, OnSyncBranch, run_passes,
    run_pre_sync_hooks, stage_again, sync_settings, SyncContext, SyncPass, SyncSettings,
)


@dataclass(frozen=True)
class _Intersync:
    """A file changed while the sync was interrupted: the base its change is measured from, "have" or a commit, and
    how it goes back to what the sync expects there: "have", "remove", or "restore" to the recorded tip's content."""
    path: str
    base: str
    back: str


@dataclass(frozen=True)
class _PutRight:
    """What a resume puts right before its passes, as stored paths: files to write again as their have revision,
    files p4 deleted that are back, p4's leftovers, the changes made while it was interrupted, the pass's tracked
    files made writable since without an edit, and its git-ignored ones made writable since, which are kept."""
    resync: list[str]
    remove: list[str]
    leftovers: list[str]
    intersync: list[_Intersync] = field(default_factory=list)
    read_only: list[str] = field(default_factory=list)
    ignore: list[str] = field(default_factory=list)


def _pass_of(progress_pass: ProgressPass) -> SyncPass:
    return SyncPass(progress_pass.changelist, progress_pass.label, progress_pass.exact)


def _p4_leftovers(interrupted: ProgressPass, lists: PassLists, workspace_dir: str) -> tuple[set[str], set[str]]:
    """What p4 left of the files the interrupted pass had not finished: temp files beside its files, and adds p4
    has not recorded that its stored lists don't account for, as an ignored file or one added on both sides, and
    whose content begins the depot revision p4 was writing; (those, and the adds on disk that are the user's).

    An abort's pass adds back the files the pass it gives up deleted, so a deleted file on disk is a candidate too."""
    accounted = set(lists.ignored) | {
        change.path for change in lists.changed if change.added_both}
    on_disk = [local_path(path, workspace_dir) for mode, path in lists.files
               if mode in ('add', 'del') and path not in accounted and os.path.lexists(local_path(path, workspace_dir))]
    log.heading('Finding what p4 has left to sync')
    revision = _pass_of(interrupted).revision
    pending = p4_sync_pending(on_disk, revision, workspace_dir)
    log.success(f'{plural(len(pending), "file")} on disk not synced yet')
    leftovers: set[str] = set()
    users: set[str] = set()
    for entry in pending:
        if entry.mode == 'add':
            p4s = _begins_depot_revision(
                entry.filepath, revision, workspace_dir)
            (leftovers if p4s else users).add(
                workspace_path(entry.filepath, workspace_dir))
    for folder in {path.rpartition('/')[0] for _, path in lists.files}:
        try:
            names = os.listdir(local_path(folder, workspace_dir)
                               if folder else workspace_dir)
        except FileNotFoundError:
            continue
        leftovers.update(
            f'{folder}/{name}' if folder else name for name in names if is_p4_temp_file(name))
    return leftovers, users


# A user's file that matches the depot revision this far is taken as p4's: the compare reads no more of either.
_COMPARED_BYTES = 1 << 20


def _begins_depot_revision(filepath: str, revision: str, workspace_dir: str) -> bool:
    """Whether the file on disk holds the start of its depot content at revision, as p4 leaves a file it was writing
    when it was stopped, line endings aside, as p4 writes the client's; up to _COMPARED_BYTES of each."""
    depot = p4_print_start(filepath, revision, _COMPARED_BYTES, workspace_dir)
    if depot is None:
        return False
    with open(filepath, 'rb') as f:
        written = f.read(_COMPARED_BYTES).replace(b'\r\n', b'\n')
    # A file cut between \r and \n ends in a lone \r.
    return depot.replace(b'\r\n', b'\n').startswith(written.removesuffix(b'\r'))


def _sort_files(progress: SyncProgress, backend: Backend, clobber_mode: ClobberMode, workspace_dir: str) -> _PutRight:
    """Sort the files against the interrupted sync: those it synced, those behind the have-list, and p4's leftovers.

    Every other file changed since is one the user changed while the sync was interrupted."""
    interrupted = progress.passes[0] if progress.passes else None
    lists = interrupted.lists if interrupted else None
    leftovers, users_adds = _p4_leftovers(
        interrupted, lists, workspace_dir) if interrupted and lists else (set(), set())
    log.heading('Checking the files against the interrupted sync')
    # A catch-up pass's files are uncommitted until the next commit, and the interrupted pass's entry wins.
    modes = {path: mode for mode, path in progress.uncommitted}
    if lists:
        modes.update((path, mode) for mode, path in lists.files)
    merged = {merged.path for merged in [
        *progress.merged, *progress.intersync_merged]}
    changed = backend.files_changed_since(progress.tip)
    local = {path: local_path(path, workspace_dir) for path in modes}
    # Looked up path by path: a listing reads the whole tree, and pathspecs match every entry against every path.
    tracked = {path for (_, path), oid in get_blob_oids(
        [(progress.tip, path) for path in sorted(set(modes) | changed)], workspace_dir).items() if oid}
    # Git-ignored files are left to p4's clobber rules, as in a normal sync; but an add p4 made that is gone again,
    # by a git clean or a checkout, is neither changed nor tracked, and p4 would not send it again.
    judged = [path for path in modes if path in changed or path in tracked
              or modes[path] != 'del' and not os.path.lexists(local[path])]
    differing, lacking = p4_off_have(
        [local[path] for path in judged], workspace_dir)
    off_have = lacking | differing
    synced: list[str] = []
    resync: list[str] = []
    remove: list[str] = []
    unsorted: list[str] = []
    in_pass = {path for _, path in lists.files} if lists else set()
    # noclobber refuses a writable file, and a resumed pass doesn't prepare its files again, as a fresh one would.
    noclobber = clobber_mode is ClobberMode.NOCLOBBER
    read_only: list[str] = []
    for path in sorted(changed | set(judged)):
        if path in leftovers or path in changed and path in merged:
            continue
        on_disk = path in local and os.path.lexists(local[path])
        if path not in changed:
            # Clean in git: put back by a reset, or a local change p4 had not reached yet.
            if local[path] in differing:
                resync.append(path)
            elif modes[path] == 'del' and on_disk and local[path] in lacking:
                remove.append(path)
            elif modes[path] != 'del' and not on_disk and local[path] not in lacking:
                resync.append(path)
            elif noclobber and path in in_pass and on_disk and is_user_writable(local[path]):
                read_only.append(path)
            continue
        if path not in modes:
            unsorted.append(path)
        elif modes[path] == 'del' and not on_disk and local[path] in lacking:
            synced.append(path)
        elif modes[path] != 'del' and on_disk and local[path] not in off_have:
            synced.append(path)
        else:
            unsorted.append(path)
    log.success(f'{plural(len(synced), "file")} synced by the interrupted run')
    # Until a resume puts the files right, a file p4 has not reached is at the recorded tip's content, which an edit
    # is made against; after, at its have revision.
    pending: set[str] = set()
    if interrupted and lists and not lists.put_right:
        pending = {workspace_path(entry.filepath, workspace_dir) for entry in p4_sync_pending(
            [local[path] for path in unsorted if path in in_pass], _pass_of(interrupted).revision, workspace_dir)}
    intersync = [_Intersync(path, progress.tip if path in pending else 'have',
                            'remove' if local[path] in lacking else 'have') if path in modes
                 else _Intersync(path, progress.tip, 'restore' if path in tracked else 'remove')
                 for path in unsorted]
    # A save holds file content, which a link would lose or, dangling, fail on.
    _refuse_links([change.path for change in intersync if os.path.islink(
        local_path(change.path, workspace_dir))])
    # git lists an untracked repository as its folder, as it does a tracked file now a folder; a save holds only files.
    _refuse_folders([change.path for change in intersync if os.path.isdir(
        local_path(change.path, workspace_dir))])
    # Git has no copy of a git-ignored file to tell an edit by, so one made writable since is kept, as a fresh pass
    # keeps it: its refusal is expected, and the end lists it as not synced.
    # Of the adds, only those the user made where p4 had still to add one.
    ignore = sorted(path for mode, path in lists.files if (mode != 'add' or path in users_adds) and path not in tracked
                    and path not in changed and path not in lists.ignored and is_user_writable(local[path])
                    ) if noclobber and lists else []
    return _PutRight(resync, remove, sorted(leftovers), intersync, read_only, ignore)


def _refuse_links(paths: list[str]) -> None:
    """Raise CommandError, after listing them, for symlinks made or changed while the sync was interrupted."""
    if paths:
        for path in paths:
            log.info(path)
        raise CommandError('The files above are symlinks made or changed while the sync was interrupted, which '
                           'git-p4son cannot keep. Remove them or put them back, then run "git p4son sync" again, '
                           f'{GIVE_UP}.')


def _refuse_folders(paths: list[str]) -> None:
    """Raise CommandError, after listing them, for folders made while the sync was interrupted."""
    if paths:
        for path in paths:
            log.info(path)
        raise CommandError('The folders above were made while the sync was interrupted where git-p4son can keep only '
                           'files: a git repository, or a folder in place of a file. Remove them or move them out of '
                           f'the workspace, then run "git p4son sync" again, {GIVE_UP}.')


def _log_paths(heading: str, paths: list[str]) -> None:
    log.heading(heading)
    for path in paths:
        log.info(path)


def _remove(paths: list[str], workspace_dir: str) -> None:
    local = local_paths(paths, workspace_dir)
    # Windows refuses to remove a read-only file, as p4 leaves synced ones.
    make_writable([path for path in local if os.path.lexists(path)])
    for path in local:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


def _on_disk(paths: list[str], prefix: str, workspace_dir: str) -> dict[str, str]:
    """Each of paths that is on disk, at prefix in a save's tree, to its local path."""
    local = {path: local_path(path, workspace_dir) for path in paths}
    return {prefix + path: local[path] for path in paths if os.path.lexists(local[path])}


_UNWOUND_MESSAGE = 'git-p4son: commits unwound while a sync was interrupted'
_SAVE_MESSAGE = 'git-p4son: changes made while a sync was interrupted'


def _save_intersync(changes: list[_Intersync], progress: SyncProgress, workspace_dir: str) -> None:
    """Save the changes made while the sync was interrupted under ours/, and record the save, before the files are
    put back."""
    if not changes:
        return
    _log_paths(f'Saving {plural(len(changes), "file")} changed since the interrupted sync',
               [change.path for change in changes])
    head = commit_files(_on_disk([change.path for change in changes], 'ours/', workspace_dir), _SAVE_MESSAGE,
                        workspace_dir, parent=progress.intersync[-1].commit if progress.intersync else None)
    set_ref(INTERSYNC_REF, head, workspace_dir)
    progress.intersync.append(IntersyncSave(
        head, [IntersyncFile(c.path, c.base) for c in changes]))
    write_sync_progress(progress, workspace_dir)
    log.success(f'{plural(len(changes), "file")} saved')


def _complete_saves(progress: SyncProgress, workspace_dir: str) -> None:
    """Keep under have/ the have revision's content of every save not yet complete, from its files as they were put
    back, as no commit holds it; an interruption can cut a save short before this."""
    incomplete = [save for save in progress.intersync if not save.complete]
    if not incomplete:
        return
    head = progress.intersync[-1].commit
    for save in incomplete:
        have = _on_disk(
            [file.path for file in save.files if file.base == 'have'], 'have/', workspace_dir)
        head = save.commit = commit_files(
            have, _SAVE_MESSAGE, workspace_dir, parent=head, tree_from=save.commit)
        save.complete = True
    set_ref(INTERSYNC_REF, head, workspace_dir)
    write_sync_progress(progress, workspace_dir)


def _saved_changes(progress: SyncProgress, workspace_dir: str) -> list[IntersyncChange]:
    """The saved changes, oldest save first, as their saves hold them."""
    keys = [(file, (save.commit, f'ours/{file.path}'),
             (save.commit, f'have/{file.path}') if file.base == 'have' else (file.base, file.path))
            for save in progress.intersync for file in save.files]
    contents = get_files_at_commits(
        [key for _, ours, base in keys for key in (ours, base)], workspace_dir)
    return [IntersyncChange(local_path(file.path, workspace_dir), contents[ours], contents[base])
            for file, ours, base in keys]


def _put_right(put: _PutRight, tip: str, workspace_dir: str, dry_run: bool = False) -> None:
    """Write the files behind the have-list again, remove those p4 deleted that are back, clear p4's leftovers, put
    the saved changes' files back as the sync expects them, the recorded tip's content outside the pass, and make the
    pass's files made writable since read-only again; with dry_run, only list them, with the changes it would save."""
    if dry_run and put.intersync:
        _log_paths('Changes made since the interrupted sync, to save', [
                   change.path for change in put.intersync])
    behind = put.resync + put.remove
    if behind:
        _log_paths('Files behind the have-list to resync' if dry_run else 'Resyncing the files behind the have-list',
                   behind)
    if not dry_run:
        p4_sync_to_have(local_paths(put.resync + [c.path for c in put.intersync if c.back == 'have'],
                                    workspace_dir), workspace_dir)
        _remove(
            put.remove + [c.path for c in put.intersync if c.back == 'remove'], workspace_dir)
        restore_files(
            tip, [c.path for c in put.intersync if c.back == 'restore'], workspace_dir)
        if behind:
            log.success(f'{plural(len(behind), "file")} resynced')
    if put.leftovers:
        _log_paths(
            "p4's leftovers to clear" if dry_run else "Clearing p4's leftovers", put.leftovers)
        if not dry_run:
            _remove(put.leftovers, workspace_dir)
            log.success(f'{plural(len(put.leftovers), "file")} cleared')
    if put.read_only:
        _log_paths('Files made writable since, to make read-only again' if dry_run
                   else 'Making read-only again the files made writable since', put.read_only)
        if not dry_run:
            make_read_only(local_paths(put.read_only, workspace_dir))
            log.success(f'{plural(len(put.read_only), "file")} made read-only')
    if put.ignore:
        _log_paths('Git-ignored files made writable since, to keep' if dry_run
                   else 'Keeping the git-ignored files made writable since', put.ignore)


def _record_put_right(progress: SyncProgress, ignore: list[str], workspace_dir: str) -> None:
    """Record on the interrupted pass that its files are put right, with ignore among its stored ignored files, so
    its p4 sync expects their refusal."""
    lists = progress.passes[0].lists if progress.passes else None
    if lists is not None:
        lists.ignored = [*lists.ignored, *ignore]
        lists.put_right = True
        write_sync_progress(progress, workspace_dir)


def _log_unwound(commits: list[AboveCommit]) -> None:
    """Name each commit unwound with how to get it back."""
    for commit in commits:
        log.info(f'{commit.back}  {commit.subject}')


def _sort_commits(progress: SyncProgress, subject: str | None,
                  backend: Backend) -> tuple[str | None, list[AboveCommit]]:
    """Sort the commits above the recorded tip: the next pass's own, made just before the interruption could record
    it, with subject, and the user's, to unwind; (the crash window's commit or None, the user's commits).

    Raises CommandError, after listing them, for other sync commits, which make the sync progress stale."""
    above = backend.commits_above(progress.tip)
    if not above:
        return None, []
    log.heading('Sorting the commits above the recorded tip')
    crash = above[0].commit if subject is not None and above[0].subject == subject else None
    user = above[1:] if crash else above
    stale = [commit for commit in user if is_sync_subject(commit.subject)]
    if stale:
        for commit in stale:
            log.info(commit.subject)
        raise StaleSyncProgress()
    if crash:
        log.info(f'{subject}: the interrupted pass\'s own commit')
    _log_unwound(user)
    log.success(
        f'{plural(len(user), "commit")} to unwind' if user else 'The pass\'s commit counts as made')
    return crash, user


def _take_crash_commit(progress: SyncProgress, commit: str, workspace_dir: str) -> None:
    """Count commit, the interrupted pass's own, as the pass made, and record it."""
    lists = progress.passes[0].lists
    assert lists is not None, 'a pass is recorded with its lists before its p4 sync'
    # What p4 refused in that pass is unknown, and so not reported.
    prep = WritableSyncFileSet(synced=local_paths(
        [path for _, path in lists.files], workspace_dir))
    ProgressRecorder(progress, workspace_dir, is_writable_mode(
        workspace_dir)).pass_made(prep, commit)


def _unwind(progress: SyncProgress, commits: list[AboveCommit], at: str, backend: Backend, workspace_dir: str) -> None:
    """Keep the user's commits above the tip under a ref, beside those an earlier resume of the sync unwound, record
    them, then take the sync branch back to at."""
    log.heading(
        f'Unwinding {plural(len(commits), "commit")} made since the interrupted sync')
    unwound = list(progress.unwound)
    known = {commit.identity: at_index for at_index,
             commit in enumerate(unwound)}
    changed = False
    for commit in commits:
        if commit.identity not in known:
            unwound.append(commit)
            changed = True
        elif unwound[known[commit.identity]].commit != commit.commit:
            # jj rewrote the change since: name its latest version.
            unwound[known[commit.identity]] = commit
            changed = True
    if changed:
        top = commits[-1].commit
        if progress.unwound:
            # The ref keeps what the earlier resume unwound, so this one joins it.
            with contextlib.suppress(RevisionNotFoundError):
                top = join_commits([resolve_commit(INTERSYNC_COMMITS_REF, workspace_dir), top], _UNWOUND_MESSAGE,
                                   workspace_dir)
        set_ref(INTERSYNC_COMMITS_REF, top, workspace_dir)
    progress.unwound = unwound
    # Before the move: a rerun finds the same commits above the tip, where after it nothing would name them.
    write_sync_progress(progress, workspace_dir)
    backend.resume_at(at)
    log.success(
        f'Kept under {INTERSYNC_COMMITS_REF}; their changes are merged back at the end')


def _abort(progress: SyncProgress, state: SyncState, workspace_dir: str) -> None:
    """Replace the passes progress has left with the catch-up to state, uncommitted, and record them, so the resume
    that runs them, now or after an interruption, is the abort; none when no file moved since the last commit, as
    neither the interrupted pass's p4 sync nor a catch-up pass's had run.

    The first keeps the interrupted pass's lists, its modes as the pass saw them, which a later sorting reads against
    the recorded tip: every other file is at the last sync already, as each sync catches up to it first, so going back
    there moves only the files that pass changed."""
    interrupted = progress.passes[0].lists if progress.passes else None
    catch_up = catch_up_passes(
        state) if interrupted is not None or progress.uncommitted else []
    progress.passes = [ProgressPass(
        p.changelist, p.label, p.exact) for p in catch_up]
    if progress.passes:
        progress.passes[0].lists = interrupted
    progress.aborting = True
    write_sync_progress(progress, workspace_dir)


def _resume_sync_passes(progress: SyncProgress, settings: SyncSettings, workspace_dir: str, backend: Backend) -> None:
    """Run the passes progress has left, the interrupted one with its stored lists, then the end over every pass's
    lists, keeping progress on disk as they go."""
    recorder = ProgressRecorder(
        progress, workspace_dir, is_writable_mode(workspace_dir))
    pre_sync_commit = progress.pre_sync_commit
    with tempfile.TemporaryDirectory(prefix='git-p4son-sync-') as temp_root:
        ctx = SyncContext(workspace_dir, settings, pre_sync_commit, temp_root,
                          files_changed_outside_syncs(workspace_dir, pre_sync_commit), recorder)
        made = WritableSyncFileSet(
            changed=stage_again(progress.changed, ctx),
            ignored=local_paths(progress.ignored, workspace_dir),
            not_synced=local_paths(progress.not_synced, workspace_dir),
            synced=local_paths(progress.synced, workspace_dir))
        merged_before = [(local_path(merged.path, workspace_dir), merged.outcome)
                         for merged in progress.merged]
        intersync_before = [(local_path(merged.path, workspace_dir), merged.outcome)
                            for merged in progress.intersync_merged]
        passes = [(_pass_of(p), p.subject) for p in progress.passes]
        resumed = progress.passes[0].lists if progress.passes else None
        intersync = _saved_changes(progress, workspace_dir)
        end_sync([made, *run_passes(passes, ctx, backend, resumed)],
                 ctx, merged_before, intersync, intersync_before)
    delete_ref(INTERSYNC_REF, workspace_dir)


def _check_no_plan_options(args: argparse.Namespace) -> None:
    """Raise CommandError for the options that make a sync's plan, which a resume runs as the interrupted sync made it,
    even ones that repeat it."""
    given = [option for option, value in (
        (' '.join(args.changelist),
         args.changelist), ('--branch', args.branch is not None),
        ('-u', args.split_user), ('--no-split',
                                  args.no_split), ('--allow-backwards', args.allow_backwards),
        ('--force', args.force)) if value]
    if given:
        them = 'it' if len(given) == 1 else 'them'
        raise CommandError(f'{", ".join(given)} would change the plan of the interrupted sync, which a resume runs as '
                           f'it was made. Run "git p4son sync" without {them} to finish the interrupted sync, '
                           f'or "git p4son sync --abort" without {them} to give it up.')


def _drop_stale_progress(progress: SyncProgress, workspace_dir: str, dry_run: bool) -> int:
    """Remove the stale sync progress, leaving the sync branch and the files as the later syncs left them, and name the
    changes it saved, which only the intersync ref keeps then."""
    log.heading(
        'The stale sync progress to remove' if dry_run else 'Removing the stale sync progress')
    if not dry_run:
        remove_sync_progress(workspace_dir)
    log.success(
        'The sync branch and the files stay as the later syncs left them')
    saved = sorted(
        {file.path for save in progress.intersync for file in save.files})
    if saved:
        log.warning(f'The changes made while the sync was interrupted stay saved under {INTERSYNC_REF}, each as '
                    'ours/<path>:')
        for path in saved:
            log.info(path)
    return 0


def resume_interrupted_sync(args: argparse.Namespace, progress: SyncProgress) -> int:
    """Finish the sync progress holds, or with --abort give it up: refuse the options that make a plan, check where it
    left the sync branch and its files, run the pre-sync hooks, then the passes left and the end."""
    _check_no_plan_options(args)
    workspace_dir = args.workspace_dir
    backend = args.backend
    # [start] and the sync branch's shape are the backend's, as much a part of the plan as its passes.
    recorded = progress.start['backend']
    if recorded != backend.words.tool:
        raise CommandError(f'The interrupted sync ran with the {recorded} backend. Run "git p4son --backend '
                           f'{recorded} sync" to finish it, or "git p4son --backend {recorded} sync --abort" to give '
                           'it up.')
    subject = progress.passes[0].subject if progress.passes else None
    try:
        sync_revision = backend.resume_sync(
            progress.sync_branch, progress.tip, progress.start, subject)
        crash, unwind = _sort_commits(progress, subject, backend)
    except StaleSyncProgress:
        if not args.abort:
            raise
        return _drop_stale_progress(progress, workspace_dir, args.dry_run)
    passes = progress.passes
    # The crash window's commit counts as made, so an abort goes back to it.
    abort_to = sync_state(
        workspace_dir, crash or progress.tip) if args.abort and passes else None
    if abort_to is not None:
        if abort_to.last_synced is None:
            raise CommandError('The interrupted sync is the workspace\'s first, so there is no synced changelist to '
                               'go back to. Run "git p4son sync" to finish it.')
        log.heading(f'Aborting the interrupted sync: back to {abort_to}')
    elif progress.aborting:
        log.heading(
            f'Finishing the interrupted abort: back to {sync_state(workspace_dir, progress.tip)}')
    else:
        left = (f'{plural(len(passes), "changelist")} left, to CL {passes[-1].changelist}' if passes
                else 'only the merge-back is left')
        log.heading(f'Resuming the interrupted sync: {left}')
    log.success(
        f'Started {progress.started:%Y-%m-%d %H:%M} UTC by git-p4son {progress.written_by}')

    depot_root = progress.depot_root
    check_no_blocking_processes(workspace_dir, args.ignore_blocking_processes)
    check_p4_workspace_clean(depot_root, workspace_dir, sync_revision)
    # The client spec's line endings and clobber option, with the depot root as the interrupted sync planned it.
    settings = dataclasses.replace(sync_settings(
        workspace_dir), depot_root=depot_root)
    put = _sort_files(progress, backend, settings.clobber_mode, workspace_dir)
    if args.dry_run:
        _put_right(put, progress.tip, workspace_dir, dry_run=True)
        log.heading('Where sync returns to')
        log.success(backend.returns_to() or f'Stays on {progress.sync_branch}')
        return 0

    last_synced = sync_state(
        workspace_dir, progress.pre_sync_commit).last_synced
    run_pre_sync_hooks(last_synced, depot_root,
                       workspace_dir, backend.words.tool)

    def run() -> SyncState:
        # The sync branch moves before the records: jj's next resume takes a bookmark on the pass's commit, not one
        # behind it.
        at = crash or progress.tip
        if unwind:
            _unwind(progress, unwind, at, backend, workspace_dir)
        else:
            backend.resume_at(at)
        if crash:
            _take_crash_commit(progress, crash, workspace_dir)
        _save_intersync(put.intersync, progress, workspace_dir)
        _put_right(put, progress.tip, workspace_dir)
        _record_put_right(progress, put.ignore, workspace_dir)
        _complete_saves(progress, workspace_dir)
        if abort_to is not None:
            _abort(progress, abort_to, workspace_dir)
        _resume_sync_passes(progress, settings, workspace_dir, backend)
        if progress.unwound:
            log.heading('The commits unwound, now changes to review')
            _log_unwound(progress.unwound)
        return sync_state(workspace_dir)

    return finish_on_branch(OnSyncBranch.from_args(args, sync_revision), depot_root, last_synced, run)
