"""A sync's checks, passes and end on the sync branch, which sync, its resume and cherry-pick share."""

import argparse
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .backend import Backend
from .common import CommandError, RunError
from .git import (
    SyncState, files_changed_outside_syncs, format_cherry_pick_subject, format_sync_subject, get_head_commit,
    get_tracked_files, sync_state,
)
from .hooks import run_hook
from .depot import resolve_depot_root
from .log import log, plural
from .processes import check_no_blocking_processes
from .sync_progress import (
    GIVE_UP, LocalChange, NoProgress, PassLists, ProgressPass, ProgressRecorder, SyncProgress, local_path, local_paths,
    progress_path, workspace_path,
)
from .sync_merge import (
    ChangedFile, ChangedFileRef, IntersyncChange, WritableSyncFileSet, merge_changed_files, merge_intersync_changes,
    prepare_writable_files, report_not_synced, stage_changed_files,
)
from .writable import check_not_allwrite, is_writable_mode, restore_writable
from .perforce import (
    ClobberMode, p4_get_opened_files, p4_sync, p4_sync_preview, P4SyncAbortError, P4SyncClobberError,
)


LAST_SYNCED_LABEL = 'last synced'


@dataclass(frozen=True)
class SyncPass:
    """A changelist to sync to, or with exact only the files it submitted (p4 sync @=N); label names it."""
    changelist: int
    label: str
    exact: bool = False

    @property
    def revision(self) -> str:
        return f'@={self.changelist}' if self.exact else f'@{self.changelist}'

    def subject(self, depot_root: str) -> str:
        """The subject of the commit for this pass."""
        return (format_cherry_pick_subject if self.exact else format_sync_subject)(depot_root, self.changelist)


def catch_up_passes(state: SyncState) -> list[SyncPass]:
    """The passes that bring the workspace back to state: its last sync, then each cherry-pick since, in order."""
    if state.last_synced is None:
        return []
    return [SyncPass(state.last_synced, LAST_SYNCED_LABEL),
            *(SyncPass(changelist, 'cherry-picked', exact=True) for changelist in state.cherry_picks)]


def find_sync_state(workspace_dir: str, sync_revision: str) -> SyncState:
    """Where the sync branch at sync_revision is synced to, under a heading."""
    log.heading(f'Finding {LAST_SYNCED_LABEL} changelist')
    state = sync_state(workspace_dir, sync_revision)
    if state.last_synced:
        log.success(str(state))
    else:
        log.warning('No previous sync found')
    return state


@dataclass(frozen=True)
class SyncSettings:
    """What every sync of a workspace reads from its depot root and client spec."""
    depot_root: str
    uses_crlf: bool
    clobber_mode: ClobberMode


def sync_settings(workspace_dir: str) -> SyncSettings:
    """The workspace's depot root and the client spec's line endings and clobber option.

    Raises CommandError for a client spec with the allwrite option."""
    resolved = resolve_depot_root(workspace_dir)
    client_spec = resolved.client_spec
    check_not_allwrite(client_spec)
    return SyncSettings(resolved.depot_root, bool(client_spec and client_spec.uses_crlf),
                        client_spec.clobber_mode if client_spec else ClobberMode.NOCLOBBER)


def check_p4_workspace_clean(depot_root: str, workspace_dir: str, sync_revision: str) -> None:
    """Raise CommandError, after listing them, if files the sync branch tracks are opened in Perforce."""
    log.heading('Checking files opened in Perforce')
    opened_files = p4_get_opened_files(depot_root, workspace_dir)
    tracked = get_tracked_files(
        [f for f, _ in opened_files], workspace_dir, sync_revision)
    tracked_opened_files = [(f, change) for f, change in opened_files
                            if f in tracked]
    if tracked_opened_files:
        for filename, change in tracked_opened_files:
            log.file_change(filename, change)
        raise CommandError(
            'Workspace has p4-opened files tracked by git. Revert them first: '
            '"p4 revert -a" reverts the unchanged ones and leaves edited ones opened.')
    if opened_files:
        for filename, change in opened_files:
            log.file_change(filename, change)
        log.warning(
            f'Ignoring {plural(len(opened_files), "p4-opened file")} '
            'not tracked by git')
    else:
        log.success('Clean')


def run_pre_sync_hooks(last_synced: int | None, depot_root: str, workspace_dir: str, backend: str) -> None:
    """Run the pre-sync hooks; raise CommandError if any of them failed."""
    if run_hook('pre-sync', workspace_dir, backend, last_synced, depot_root):
        raise CommandError('Stopping before anything is synced, because a pre-sync hook failed. '
                           'See its output above.')


def sync_preflight(depot_root: str, workspace_dir: str, ignore_blocking_processes: bool,
                   backend: Backend, sync_revision: str) -> None:
    """Gate a sync on blocking processes and clean workspaces; raise CommandError if one fails."""
    check_no_blocking_processes(workspace_dir, ignore_blocking_processes)
    backend.check_clean()
    check_p4_workspace_clean(depot_root, workspace_dir, sync_revision)


@dataclass(frozen=True)
class SyncContext:
    """What every pass of one sync shares."""
    workspace_dir: str
    settings: SyncSettings
    pre_sync_head_commit: str
    temp_root: str
    # A committed local change can be read-only, as git p4son writable off leaves it.
    changed_locally: set[str]
    recorder: ProgressRecorder | NoProgress


def _check_refused(refused: list[str], expected: set[str], returncode: int, workspace_dir: str) -> None:
    """Raise CommandError, after listing them, if p4 refused files not in expected."""
    expected_paths = {workspace_path(f, workspace_dir) for f in expected}
    unexpected = [f for f in refused
                  if workspace_path(f, workspace_dir) not in expected_paths]
    if unexpected:
        for f in unexpected:
            log.info(f)
        raise CommandError(
            'p4 unexpectedly refused to replace the files above', returncode=returncode)
    if refused:
        log.warning(plural(len(refused), 'expected refusal'))


def _sync_changelist(sync_pass: SyncPass, depot_root: str, workspace_dir: str,
                     expected_refusals: set[str]) -> None:
    """Run sync_pass's p4 sync, in which p4 may refuse the expected_refusals files.

    Any other refused file raises.
    """
    changelist, label = sync_pass.changelist, sync_pass.label
    log.heading(f'Syncing the files of {label} CL {changelist}' if sync_pass.exact
                else f'Syncing to {label} CL ({changelist})')
    try:
        p4_sync(sync_pass.revision, depot_root, workspace_dir)
    except P4SyncClobberError as e:
        _check_refused(e.refused, expected_refusals,
                       e.returncode, workspace_dir)
    except P4SyncAbortError as e:
        log.info(str(e))
        then = (f'run "git p4son sync" to finish, {GIVE_UP}' if os.path.exists(progress_path(workspace_dir))
                else 'commit or restore the synced files before syncing again')
        raise CommandError(
            'Aborting sync: p4 could not replace a file, most likely because '
            'another process has it open. The workspace is left partially '
            f'synced: close that process, then {then}.') from e


def _run_pass(sync_pass: SyncPass, prep: WritableSyncFileSet, files: list[str], ctx: SyncContext) -> None:
    """Run sync_pass's p4 sync of files, prepared as prep, and fill in what it synced."""
    if not files:
        return
    _sync_changelist(sync_pass, ctx.settings.depot_root,
                     ctx.workspace_dir, expected_refusals=set(prep.ignored))
    prep.synced = files


def _sync_pass(sync_pass: SyncPass, ctx: SyncContext) -> WritableSyncFileSet:
    """Run one sync pass: preview, prepare writable files, and p4 sync."""
    settings = ctx.settings
    log.heading(f'Previewing sync of CL {sync_pass.changelist}\'s files' if sync_pass.exact
                else f'Previewing sync to CL {sync_pass.changelist}')
    preview = p4_sync_preview(
        sync_pass.revision, settings.depot_root, ctx.workspace_dir)
    prep = prepare_writable_files(preview, ctx.workspace_dir,
                                  ctx.pre_sync_head_commit, ctx.temp_root,
                                  uses_crlf=settings.uses_crlf,
                                  clobber_mode=settings.clobber_mode,
                                  changed_locally=ctx.changed_locally)
    ctx.recorder.before_sync(preview, prep)
    _run_pass(sync_pass, prep, [entry.filepath for entry in preview], ctx)
    return prep


def stage_again(changes: list[LocalChange], ctx: SyncContext) -> list[ChangedFile]:
    """Stage again the local changes the sync progress keeps by reference."""
    workspace_dir = ctx.workspace_dir
    return stage_changed_files(
        [ChangedFileRef(local_path(change.path, workspace_dir), change.base, change.binary, change.added_both)
         for change in changes],
        ctx.pre_sync_head_commit, workspace_dir, ctx.temp_root, ctx.settings.uses_crlf)


def _resume_pass(sync_pass: SyncPass, lists: PassLists, ctx: SyncContext) -> WritableSyncFileSet:
    """Run the interrupted pass again with the lists it stored, as classifying its files again would read the ones it
    made read-only as unchanged."""
    workspace_dir = ctx.workspace_dir
    prep = WritableSyncFileSet(
        changed=stage_again(lists.changed, ctx),
        ignored=local_paths(lists.ignored, workspace_dir))
    _run_pass(sync_pass, prep, local_paths(
        [path for _, path in lists.files], workspace_dir), ctx)
    return prep


def run_passes(passes: list[tuple[SyncPass, str | None]], ctx: SyncContext, backend: Backend,
               resumed: PassLists | None = None) -> list[WritableSyncFileSet]:
    """Run each pass, committing the target ones, those with the subject of their commit; their preparations.

    With resumed, the first pass is the interrupted one, run with the lists it stored."""
    preps: list[WritableSyncFileSet] = []
    # Local changes are merged back once at the end, so each commit is pure Perforce state.
    for sync_pass, subject in passes:
        prep = (_resume_pass(sync_pass, resumed, ctx) if resumed is not None and not preps
                else _sync_pass(sync_pass, ctx))
        preps.append(prep)
        if subject is None:
            ctx.recorder.pass_made(prep)
            continue
        if sync_pass.exact and not prep.synced:
            log.warning(
                f'CL {sync_pass.changelist} changes no files in the workspace')

        log.heading(f'Committing git changes for CL {sync_pass.changelist}')
        staged = backend.commit_sync(subject)
        log.success(f'Committed {plural(staged, "file")}')
        ctx.recorder.pass_made(prep, get_head_commit(ctx.workspace_dir))
    return preps


def _make_synced_files_writable(synced: list[str], workspace_dir: str) -> None:
    """In writable mode, make the git-tracked files this sync wrote writable."""
    if not synced or not is_writable_mode(workspace_dir):
        return
    log.heading('Making synced tracked files writable (writable mode)')
    synced = sorted(set(synced))
    tracked = get_tracked_files(synced, workspace_dir)
    changed = restore_writable(synced, tracked, workspace_dir)
    log.success(
        f'{changed} of {plural(len(tracked), "tracked file")} made writable')


def end_sync(preps: list[WritableSyncFileSet], ctx: SyncContext,
             merged_before: list[tuple[str, str]] | None = None,
             intersync: Sequence[IntersyncChange] = ()) -> None:
    """Merge the local changes back, then the intersync changes, report the files not synced, apply writable mode,
    and drop the sync progress.

    merged_before holds the (path, outcome) of the files an interrupted run merged back, reported with the rest."""
    workspace_dir = ctx.workspace_dir
    # Dedup files that showed up in several sync passes.
    changed = [cf for p in preps for cf in p.changed]
    by_path = {workspace_path(
        cf.filepath, workspace_dir): cf for cf in changed}
    changed_files = sorted(by_path.values(), key=lambda cf: cf.filepath)
    merge_changed_files(changed_files, workspace_dir,
                        ctx.temp_root, ctx.recorder.merged, merged_before)
    merge_intersync_changes(intersync, workspace_dir, ctx.temp_root, ctx.settings.uses_crlf,
                            ctx.recorder.intersync_merged)

    report_not_synced(preps, ctx.settings.clobber_mode, workspace_dir)

    _make_synced_files_writable(
        [f for p in preps for f in p.synced], workspace_dir)

    ctx.recorder.finish()


def _progress_recorder(catch_up: list[SyncPass], targets: list[SyncPass], depot_root: str,
                       pre_sync_head_commit: str, workspace_dir: str, backend: Backend) -> ProgressRecorder:
    """A recorder of the sync progress, every pass still to run, with the sync branch at the pre-sync commit."""
    sync_branch, start = backend.progress_start()
    passes = [ProgressPass(p.changelist, p.label, p.exact) for p in catch_up]
    passes += [ProgressPass(p.changelist, p.label, p.exact,
                            p.subject(depot_root)) for p in targets]
    progress = SyncProgress(
        sync_branch, depot_root, pre_sync_head_commit, pre_sync_head_commit, start, passes)
    return ProgressRecorder(progress, workspace_dir, is_writable_mode(workspace_dir))


def run_sync_passes(catch_up: list[SyncPass], targets: list[SyncPass], settings: SyncSettings, workspace_dir: str,
                    backend: Backend, record_progress: bool = False) -> None:
    """Sync catch_up, folded into the first target's commit, then commit each target; merge local changes.

    With record_progress, the sync progress is kept from the first p4 sync until the end has run."""
    log.heading('Finding HEAD commit')
    pre_sync_head_commit = get_head_commit(workspace_dir)
    log.success(f'{pre_sync_head_commit}')

    recorder = (_progress_recorder(catch_up, targets, settings.depot_root, pre_sync_head_commit, workspace_dir,
                                   backend) if record_progress else NoProgress())
    with tempfile.TemporaryDirectory(prefix='git-p4son-sync-') as temp_root:
        ctx = SyncContext(workspace_dir, settings, pre_sync_head_commit, temp_root,
                          files_changed_outside_syncs(workspace_dir, pre_sync_head_commit), recorder)

        passes: list[tuple[SyncPass, str | None]] = [
            (sync_pass, None) for sync_pass in catch_up]
        passes += [(target, target.subject(settings.depot_root))
                   for target in targets]
        end_sync(run_passes(passes, ctx, backend), ctx)


@dataclass(frozen=True)
class OnSyncBranch:
    """Where and how sync and cherry-pick run on the sync branch."""
    workspace_dir: str
    backend: Backend
    sync_revision: str
    ignore_blocking_processes: bool
    rebase: bool

    @classmethod
    def from_args(cls, args: argparse.Namespace, sync_revision: str) -> 'OnSyncBranch':
        return cls(args.workspace_dir, args.backend, sync_revision,
                   args.ignore_blocking_processes, not args.no_rebase)


def _run_post_sync_hooks(synced: int | None, last_synced: int | None, depot_root: str, workspace_dir: str,
                         backend: str) -> int:
    """Run the post-sync hooks of a sync that ended at synced; the exit code, 1 if a Ctrl-C or an error stopped them."""
    try:
        run_hook('post-sync', workspace_dir, backend,
                 last_synced, depot_root, synced=synced)
    except KeyboardInterrupt:
        log.error('post-sync hooks interrupted')
        return 1
    except RunError as error:
        for line in error.stderr:
            log.info(line)
        log.error(
            f'post-sync hooks stopped: {str(error).strip()} failed with return code {error.returncode}')
        return 1
    except CommandError as error:
        log.error(f'post-sync hooks stopped: {error}')
        return 1
    return 0


def finish_on_branch(on: OnSyncBranch, depot_root: str, last_synced: int | None,
                     run: Callable[[], SyncState]) -> int:
    """Run run, then the post-sync hooks with the state it gives, and return; the command's exit code.

    A failure in run leaves the sync on the sync branch, which git p4son sync finishes once there is sync progress."""
    backend = on.backend
    try:
        state = run()
        exit_code = _run_post_sync_hooks(
            state.latest, last_synced, depot_root, on.workspace_dir, backend.words.tool)
    except BaseException:
        backend.fail_sync(os.path.exists(progress_path(on.workspace_dir)))
        raise
    backend.finish_sync(str(state), on.rebase)
    return exit_code


def sync_on_branch(on: OnSyncBranch, depot_root: str, last_synced: int | None, run: Callable[[], SyncState],
                   before_run: Callable[[], None] | None = None) -> int:
    """Check out the sync branch after the preflight, run the pre-sync hooks and before_run, then run, run the
    post-sync hooks with the state run gives, and return; the command's exit code.

    last_synced, the last synced changelist, is for the hooks. A Ctrl-C or an error in the post-sync hooks stops
    them, not the return, as the sync has finished by then, and gives exit code 1."""
    backend = on.backend
    sync_preflight(depot_root, on.workspace_dir,
                   on.ignore_blocking_processes, backend, on.sync_revision)
    backend.start_sync()
    try:
        run_pre_sync_hooks(last_synced, depot_root,
                           on.workspace_dir, backend.words.tool)
        if before_run is not None:
            # Before the costly split queries, the checks and hooks have run.
            before_run()
    except BaseException:
        backend.abandon_sync()
        raise
    return finish_on_branch(on, depot_root, last_synced, run)
