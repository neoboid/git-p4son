"""Sync command implementation for git-p4son."""

import argparse
import contextlib
import dataclasses
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

from .backend import Backend
from .common import CommandError, RunError, normalize_workspace_path
from .git import (
    SyncState, files_changed_outside_syncs, format_cherry_pick_subject, format_sync_subject, get_head_commit,
    get_tracked_files, list_tracked_files, sync_state,
)
from .hooks import check_hooks, run_hook
from .depot import resolve_depot_root
from .log import log, plural
from .processes import check_no_blocking_processes
from .sync_progress import (
    LocalChange, NoProgress, PassLists, ProgressPass, ProgressRecorder, SyncProgress, local_path, progress_path,
    read_sync_progress,
)
from .sync_targets import default_sync_target, resolve_sync_target
from .sync_merge import (
    ChangedFile, ChangedFileRef, WritableSyncFileSet, merge_changed_files, prepare_writable_files,
    report_not_synced, stage_changed_files,
)
from .writable import is_writable_mode, make_writable, restore_writable
from .sync_split_users import (
    canonical_split_users,
    get_split_users,
    resolve_split_users,
)
from .perforce import (
    ClobberMode,
    get_latest_changelist,
    get_latest_changes,
    get_user_changes,
    p4_get_opened_files,
    p4_off_have,
    p4_sync_pending,
    p4_sync_to_have,
    is_p4_temp_file,
    p4_sync,
    p4_sync_preview,
    P4SyncAbortError,
    P4SyncClobberError,
    run_p4_batch,
)


LAST_SYNCED_LABEL = 'last synced'
_SPLIT_LABEL = 'split'
_SPECIFIED_LABEL = 'specified'


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


def _sync_changelist(sync_pass: SyncPass, depot_root: str, workspace_dir: str,
                     expected_refusals: set[str]) -> list[str]:
    """Run sync_pass's p4 sync, returning the expected_refusals files p4 refused.

    Any other refused file raises.
    """
    changelist, label = sync_pass.changelist, sync_pass.label
    log.heading(f'Syncing the files of {label} CL {changelist}' if sync_pass.exact
                else f'Syncing to {label} CL ({changelist})')
    try:
        refused = p4_sync(sync_pass.revision, depot_root, workspace_dir)
    except P4SyncClobberError as e:
        _check_refused(e.refused, expected_refusals,
                       e.returncode, workspace_dir)
        return e.refused
    except P4SyncAbortError as e:
        log.info(str(e))
        then = ('run "git p4son sync" to finish' if os.path.exists(progress_path(workspace_dir))
                else 'commit or restore the synced files before syncing again')
        raise CommandError(
            'Aborting sync: p4 could not replace a file, most likely because '
            'another process has it open. The workspace is left partially '
            f'synced: close that process, then {then}.') from e
    _check_refused(refused, expected_refusals, 1, workspace_dir)
    return refused


def _workspace_path(filepath: str, workspace_dir: str) -> str:
    """filepath as a slash path from the workspace root, which tells the same file however it is spelled."""
    return normalize_workspace_path(filepath, workspace_dir, allow_outside=True) or filepath


def _check_refused(refused: list[str], expected: set[str], returncode: int, workspace_dir: str) -> None:
    """Raise CommandError, after listing them, if p4 refused files not in expected."""
    expected_paths = {_workspace_path(f, workspace_dir) for f in expected}
    unexpected = [f for f in refused
                  if _workspace_path(f, workspace_dir) not in expected_paths]
    if unexpected:
        for f in unexpected:
            log.info(f)
        raise CommandError(
            'p4 unexpectedly refused to replace the files above', returncode=returncode)
    if refused:
        log.warning(plural(len(refused), 'expected refusal'))


def _check_p4_workspace_clean(depot_root: str, workspace_dir: str, sync_revision: str) -> None:
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


def _run_pre_sync_hooks(last_synced: int | None, depot_root: str, workspace_dir: str, backend: str) -> None:
    """Run the pre-sync hooks; raise CommandError if any of them failed."""
    if run_hook('pre-sync', workspace_dir, backend, last_synced, depot_root):
        raise CommandError('Stopping before anything is synced, because a pre-sync hook failed. '
                           'See its output above.')


def sync_preflight(depot_root: str, workspace_dir: str, ignore_blocking_processes: bool,
                   backend: Backend, sync_revision: str) -> None:
    """Gate a sync on blocking processes and clean workspaces; raise CommandError if one fails."""
    check_no_blocking_processes(workspace_dir, ignore_blocking_processes)
    backend.check_clean()
    _check_p4_workspace_clean(depot_root, workspace_dir, sync_revision)


@dataclass(frozen=True)
class SyncSettings:
    """What every sync of a workspace reads from its depot root and client spec."""
    depot_root: str
    uses_crlf: bool
    clobber_mode: ClobberMode


def sync_settings(workspace_dir: str) -> SyncSettings:
    """The workspace's depot root and the client spec's line endings and clobber option."""
    resolved = resolve_depot_root(workspace_dir)
    client_spec = resolved.client_spec
    return SyncSettings(resolved.depot_root, bool(client_spec and client_spec.uses_crlf),
                        client_spec.clobber_mode if client_spec else ClobberMode.NOCLOBBER)


@dataclass(frozen=True)
class _SyncContext:
    """What every pass of one sync shares."""
    workspace_dir: str
    settings: SyncSettings
    pre_sync_head_commit: str
    temp_root: str
    # A committed local change can be read-only, as git p4son writable off leaves it.
    changed_locally: set[str]
    recorder: ProgressRecorder | NoProgress


def find_sync_state(workspace_dir: str, sync_revision: str) -> SyncState:
    """Where the sync branch at sync_revision is synced to, under a heading."""
    log.heading(f'Finding {LAST_SYNCED_LABEL} changelist')
    state = sync_state(workspace_dir, sync_revision)
    if state.last_synced:
        log.success(str(state))
    else:
        log.warning('No previous sync found')
    return state


def catch_up_passes(state: SyncState) -> list[SyncPass]:
    """The passes that bring the workspace back to state: its last sync, then each cherry-pick since, in order."""
    if state.last_synced is None:
        return []
    return [SyncPass(state.last_synced, LAST_SYNCED_LABEL),
            *(SyncPass(changelist, 'cherry-picked', exact=True) for changelist in state.cherry_picks)]


def parse_changelist(raw: str) -> int:
    """The changelist number raw gives; raise CommandError if it isn't one."""
    try:
        return int(raw)
    except ValueError:
        raise CommandError(f'Invalid changelist number: {raw}') from None


def _check_increasing(changelists: list[tuple[int, str]]) -> None:
    """Raise CommandError unless the changelists, each with how the error names it, are strictly increasing."""
    for (prev, prev_named), (curr, curr_named) in zip(changelists, changelists[1:]):
        if curr <= prev:
            raise CommandError('Changelists must be strictly increasing, '
                               f'got {prev_named} then {curr_named}')


def _sync_pass(sync_pass: SyncPass, ctx: _SyncContext) -> WritableSyncFileSet:
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


def _run_pass(sync_pass: SyncPass, prep: WritableSyncFileSet, files: list[str], ctx: _SyncContext) -> None:
    """Run sync_pass's p4 sync of files, prepared as prep, and fill in what it left out and synced."""
    if not files:
        return
    workspace_dir = ctx.workspace_dir
    # allwrite refuses only files prepare_writable_files sorted as ignored, or tracked into replace.
    refused = _sync_changelist(sync_pass, ctx.settings.depot_root, workspace_dir,
                               expected_refusals=set(prep.ignored) | set(prep.replace))
    replace = {_workspace_path(f, workspace_dir): depot_path
               for f, depot_path in prep.replace.items()}
    refused_paths = [(f, _workspace_path(f, workspace_dir)) for f in refused]
    prep.not_synced = [f for f, path in refused_paths if path not in replace]
    _replace_refused([replace[path] + sync_pass.revision for _, path in refused_paths if path in replace],
                     workspace_dir)
    prep.synced = files


def _replace_refused(revisions: list[str], workspace_dir: str) -> None:
    """Sync each refused depot path, at the pass's revision, over its local changes, which the merge-back restores."""
    if not revisions:
        return
    log.heading('Replacing tracked files with local changes (allwrite)')
    run_p4_batch(['sync', '-f'], revisions,
                 'Syncing over their local changes', workspace_dir, dry_run=False)
    log.success(f'{plural(len(revisions), "file")} replaced')


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


def build_sync_targets(splits: list[tuple[int, int]], last_synced: int, required: list[int]) -> list[int]:
    """Build the sync sequence that gives each split changelist a commit of its own.

    splits pairs each changelist with the one submitted to the depot root just before it, or 0 for none, which is
    synced first when newer than last_synced."""
    targets = set(required)
    for change, before in splits:
        targets.add(change)
        if before > last_synced:
            targets.add(before)
    return sorted(targets)


def _collect_split_users(args: argparse.Namespace,
                         workspace_dir: str) -> list[str]:
    """Return the configured split users (unless --no-split) followed by the -u ones.

    Raises CommandError if a -u user is unknown.
    """
    extra = args.split_user or []
    if extra:
        extra = canonical_split_users(extra, workspace_dir)
    configured = [] if args.no_split else get_split_users(workspace_dir)
    return configured + extra


def _should_split(split_users: list[str], last_synced: int | None,
                  targets: list[tuple[int, str]]) -> bool:
    """Whether to split out the split users' changelists before the targets."""
    if not split_users or not targets:
        return False
    if not last_synced:
        log.info('Not splitting out changelists: '
                 'no previous sync to start from')
        return False
    if targets[0][0] < last_synced:
        log.info('Not splitting out changelists: '
                 'syncing to an older changelist')
        return False
    return True


def _split_targets(targets: list[tuple[int, str]], users: list[str],
                   last_synced: int, depot_root: str,
                   workspace_dir: str) -> list[tuple[int, str]]:
    """Add the targets that give each of users' changelists its own commit."""
    upper = targets[-1][0]
    log.heading(
        f'Finding split users\' changelists in CL {last_synced}..{upper}')
    candidates = {change: user for user in users
                  for change in get_user_changes(user, last_synced, upper, workspace_dir)
                  if change > last_synced}
    log.success(plural(len(candidates), 'changelist'))

    log.heading('Finding changelists to split into their own commits')
    changes = sorted(candidates)
    splits = []
    for change, latest in zip(changes, get_latest_changes(
            depot_root, [f'@{change}' for change in changes], 2, workspace_dir)):
        # A changelist that touched nothing under the depot root isn't the newest there.
        if not latest or latest[0] != change:
            continue
        splits.append((change, latest[1] if len(latest) > 1 else 0))
        log.info(f'CL {change} ({candidates[change]})')
    if splits:
        log.success(f'{plural(len(splits), "changelist")} to split out')
    else:
        log.success('None found')

    labels = dict(targets)
    return [(cl, labels.get(cl, _SPLIT_LABEL))
            for cl in build_sync_targets(splits, last_synced, list(labels))]


def _latest_target(depot_root: str, workspace_dir: str) -> tuple[int, str]:
    """Look up the latest submitted changelist as a sync target."""
    log.heading('Finding latest changelist')
    latest = get_latest_changelist(depot_root, workspace_dir)
    log.success(f'CL {latest}')
    return (latest, 'latest')


def _resolve_sync_targets(
        raw: list[str], last_synced: int | None, depot_root: str,
        workspace_dir: str, allow_backwards: bool) -> list[tuple[int, str]]:
    """Resolve the ordered (changelist, label) targets a sync will visit.

    Returns [] when already at the only target; raises CommandError for invalid arguments.
    """
    lowered = [c.lower() for c in raw]

    if 'head' in lowered and lowered.index('head') != len(lowered) - 1:
        raise CommandError('The "head" keyword must come last')

    targets = []
    resolved: dict[str, int] = {}
    for c in raw or [default_sync_target(workspace_dir) or 'head']:
        name = c.lower()
        if name == 'head':
            targets.append(_latest_target(depot_root, workspace_dir))
            continue
        try:
            targets.append((int(c), _SPECIFIED_LABEL))
        except ValueError:
            if name not in resolved:
                resolved[name] = resolve_sync_target(
                    c, last_synced, depot_root, workspace_dir)
            targets.append((resolved[name], name))

    _check_increasing([(cl, str(cl) if label == _SPECIFIED_LABEL else f'{cl} ({label})')
                      for cl, label in targets])
    numbers = [cl for cl, _ in targets]

    if last_synced and numbers[0] < last_synced:
        if not allow_backwards:
            raise CommandError(
                f'Cannot sync to CL {numbers[0]} '
                f'(currently at CL {last_synced}) without --allow-backwards')
        log.warning(
            f'Syncing to older CL {numbers[0]} '
            f'(currently at CL {last_synced})')

    if last_synced in numbers:
        log.info(f'Skipping CL {last_synced} (already synced)')
        targets = [(cl, lbl) for cl, lbl in targets
                   if cl != last_synced]

    return targets


@dataclass
class _SyncPlan:
    """A resolved sync: the targets to visit after the catch-up pass to last_synced."""
    settings: SyncSettings
    state: SyncState
    targets: list[tuple[int, str]]
    # Re-sync only the state, the cherry-picks included; targets is empty.
    resync_last_synced: bool
    split_users: list[str]
    split: bool

    @property
    def last_synced(self) -> int | None:
        return self.state.last_synced


def _plan_sync(args: argparse.Namespace, sync_revision: str) -> _SyncPlan | None:
    """Resolve what to sync from the arguments and the sync branch at sync_revision, or None when already synced.

    Raises CommandError for an invalid argument.
    """
    workspace_dir = args.workspace_dir

    settings = sync_settings(workspace_dir)
    depot_root = settings.depot_root
    state = find_sync_state(workspace_dir, sync_revision)
    last_synced = state.last_synced

    split_users = _collect_split_users(args, workspace_dir)

    lowered = [c.lower() for c in args.changelist]

    # "last-synced" re-syncs the current changelist: no targets, only the catch-up pass.
    resync_last_synced = 'last-synced' in lowered
    if resync_last_synced:
        if lowered != ['last-synced']:
            raise CommandError('The "last-synced" keyword cannot be combined '
                               'with other changelists')
        if not last_synced:
            raise CommandError(
                'No previous sync found, cannot use "last-synced"')
        targets = []
    else:
        targets = _resolve_sync_targets(
            args.changelist, last_synced, depot_root, workspace_dir, args.allow_backwards)
        if not targets:
            log.heading('Checking sync targets')
            log.success(f'Already at {state}, nothing to sync')
            return None
        final = targets[-1][0]
        taken_back = [
            changelist for changelist in state.cherry_picks if changelist > final]
        if taken_back:
            log.warning(f'Syncing to CL {final} takes back the cherry-picks of CL '
                        f'{", ".join(map(str, taken_back))}')

    return _SyncPlan(
        settings=settings, state=state, targets=targets,
        resync_last_synced=resync_last_synced, split_users=split_users,
        split=_should_split(split_users, last_synced, targets))


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
        ctx = _SyncContext(workspace_dir, settings, pre_sync_head_commit, temp_root,
                           files_changed_outside_syncs(workspace_dir, pre_sync_head_commit), recorder)

        passes: list[tuple[SyncPass, str | None]] = [
            (sync_pass, None) for sync_pass in catch_up]
        passes += [(target, target.subject(settings.depot_root))
                   for target in targets]
        _end_sync(_run_passes(passes, ctx, backend), ctx)


def _local_paths(paths: list[str], workspace_dir: str) -> list[str]:
    return [local_path(path, workspace_dir) for path in paths]


def _staged(changes: list[LocalChange], ctx: _SyncContext) -> list[ChangedFile]:
    """Stage again the local changes the sync progress keeps by reference."""
    workspace_dir = ctx.workspace_dir
    return stage_changed_files(
        [ChangedFileRef(local_path(change.path, workspace_dir), change.base, change.binary, change.added_both)
         for change in changes],
        ctx.pre_sync_head_commit, workspace_dir, ctx.temp_root, ctx.settings.uses_crlf)


def _resume_pass(sync_pass: SyncPass, lists: PassLists, ctx: _SyncContext) -> WritableSyncFileSet:
    """Run the interrupted pass again with the lists it stored, as classifying its files again would read the ones it
    made read-only as unchanged."""
    workspace_dir = ctx.workspace_dir
    prep = WritableSyncFileSet(
        changed=_staged(lists.changed, ctx),
        ignored=_local_paths(lists.ignored, workspace_dir),
        always_writable=_local_paths(lists.always_writable, workspace_dir),
        replace={local_path(path, workspace_dir): depot_path for path, depot_path in lists.replace.items()})
    _run_pass(sync_pass, prep, _local_paths(
        [path for _, path in lists.files], workspace_dir), ctx)
    return prep


def _run_passes(passes: list[tuple[SyncPass, str | None]], ctx: _SyncContext, backend: Backend,
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


def _end_sync(preps: list[WritableSyncFileSet], ctx: _SyncContext,
              merged_before: list[tuple[str, str]] | None = None) -> None:
    """Merge the local changes back, report the files not synced, apply writable mode, and drop the sync progress.

    merged_before holds the (path, outcome) of the files an interrupted run merged back, reported with the rest."""
    workspace_dir = ctx.workspace_dir
    # Dedup files that showed up in several sync passes.
    changed = [cf for p in preps for cf in p.changed]
    by_path = {_workspace_path(
        cf.filepath, workspace_dir): cf for cf in changed}
    changed_files = sorted(by_path.values(), key=lambda cf: cf.filepath)
    merge_changed_files(changed_files, workspace_dir,
                        ctx.temp_root, ctx.recorder.merged, merged_before)

    report_not_synced(preps, ctx.settings.clobber_mode, workspace_dir)

    _make_synced_files_writable(
        [f for p in preps for f in p.synced], workspace_dir)

    ctx.recorder.finish()


def resume_sync_passes(progress: SyncProgress, settings: SyncSettings, workspace_dir: str, backend: Backend) -> None:
    """Run the passes progress has left, the interrupted one with its stored lists, then the end over every pass's
    lists, keeping progress on disk as they go."""
    recorder = ProgressRecorder(
        progress, workspace_dir, is_writable_mode(workspace_dir))
    pre_sync_commit = progress.pre_sync_commit
    with tempfile.TemporaryDirectory(prefix='git-p4son-sync-') as temp_root:
        ctx = _SyncContext(workspace_dir, settings, pre_sync_commit, temp_root,
                           files_changed_outside_syncs(workspace_dir, pre_sync_commit), recorder)
        made = WritableSyncFileSet(
            changed=_staged(progress.changed, ctx),
            ignored=_local_paths(progress.ignored, workspace_dir),
            not_synced=_local_paths(progress.not_synced, workspace_dir),
            synced=_local_paths(progress.synced, workspace_dir))
        merged_before = [(local_path(merged.path, workspace_dir), merged.outcome)
                         for merged in progress.merged]
        passes = [(_pass_of(p), p.subject) for p in progress.passes]
        resumed = progress.passes[0].lists if progress.passes else None
        _end_sync([made, *_run_passes(passes, ctx, backend, resumed)],
                  ctx, merged_before)


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
        _run_pre_sync_hooks(last_synced, depot_root,
                            on.workspace_dir, backend.words.tool)
        if before_run is not None:
            # Before the costly split queries, the checks and hooks have run.
            before_run()
    except BaseException:
        backend.abandon_sync()
        raise
    return _finish_on_branch(on, depot_root, last_synced, run)


def _finish_on_branch(on: OnSyncBranch, depot_root: str, last_synced: int | None,
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


@dataclass(frozen=True)
class _PutRight:
    """What a resume puts right before its passes, as stored paths: files to write again as their have revision,
    files p4 deleted that are back, and p4's leftovers."""
    resync: list[str]
    remove: list[str]
    leftovers: list[str]


def _pass_of(progress_pass: ProgressPass) -> SyncPass:
    return SyncPass(progress_pass.changelist, progress_pass.label, progress_pass.exact)


def _p4_leftovers(interrupted: ProgressPass, lists: PassLists, workspace_dir: str) -> set[str]:
    """What p4 left of the files the interrupted pass had not finished: temp files beside its files, and adds p4
    has not recorded that its stored lists don't account for, as an ignored file or one added on both sides."""
    accounted = set(lists.ignored) | {
        change.path for change in lists.changed if change.added_both}
    on_disk = [local_path(path, workspace_dir) for mode, path in lists.files
               if mode == 'add' and path not in accounted and os.path.lexists(local_path(path, workspace_dir))]
    log.heading('Finding what p4 has left to sync')
    pending = p4_sync_pending(on_disk, _pass_of(
        interrupted).revision, workspace_dir)
    log.success(f'{plural(len(pending), "file")} on disk not synced yet')
    leftovers = {_workspace_path(entry.filepath, workspace_dir)
                 for entry in pending if entry.mode == 'add'}
    for folder in {path.rpartition('/')[0] for _, path in lists.files}:
        try:
            names = os.listdir(local_path(folder, workspace_dir)
                               if folder else workspace_dir)
        except FileNotFoundError:
            continue
        leftovers.update(
            f'{folder}/{name}' if folder else name for name in names if is_p4_temp_file(name))
    return leftovers


def _sort_files(progress: SyncProgress, backend: Backend, workspace_dir: str) -> _PutRight:
    """Sort the files against the interrupted sync: those it synced, those behind the have-list, and p4's leftovers.

    Raises CommandError, after listing them, for files changed since, which a resume can't sort yet."""
    interrupted = progress.passes[0] if progress.passes else None
    lists = interrupted.lists if interrupted else None
    leftovers = _p4_leftovers(
        interrupted, lists, workspace_dir) if interrupted and lists else set()
    log.heading('Checking the files against the interrupted sync')
    # A catch-up pass's files are uncommitted until the next commit, and the interrupted pass's entry wins.
    modes = {path: mode for mode, path in progress.uncommitted}
    if lists:
        modes.update((path, mode) for mode, path in lists.files)
    merged = {merged.path for merged in progress.merged}
    changed = backend.files_changed_since(progress.tip)
    local = {path: local_path(path, workspace_dir) for path in modes}
    tracked = set(list_tracked_files(workspace_dir, progress.tip))
    # Git-ignored files are left to p4's clobber rules, as in a normal sync.
    judged = [path for path in modes if path in changed or path in tracked]
    differing, lacking = p4_off_have(
        [local[path] for path in judged], workspace_dir)
    off_have = lacking | differing
    synced, resync, remove, unsorted = [], [], [], []
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
            continue
        if path not in modes:
            unsorted.append(path)
        elif modes[path] == 'del' and not on_disk and local[path] in lacking:
            synced.append(path)
        elif modes[path] != 'del' and on_disk and local[path] not in off_have:
            synced.append(path)
        else:
            unsorted.append(path)
    if unsorted:
        for path in unsorted:
            log.info(path)
        raise CommandError('The files above changed since the interrupted sync, which git-p4son cannot sort out '
                           'yet. Put them back as the interrupted sync left them, then run "git p4son sync" again.')
    log.success(f'{plural(len(synced), "file")} synced by the interrupted run')
    return _PutRight(resync, remove, sorted(leftovers))


def _log_paths(heading: str, paths: list[str]) -> None:
    log.heading(heading)
    for path in paths:
        log.info(path)


def _remove(paths: list[str], workspace_dir: str) -> None:
    local = _local_paths(paths, workspace_dir)
    # Windows refuses to remove a read-only file, as p4 leaves synced ones.
    make_writable([path for path in local if os.path.lexists(path)])
    for path in local:
        with contextlib.suppress(FileNotFoundError):
            os.remove(path)


def _put_right(put: _PutRight, workspace_dir: str, dry_run: bool = False) -> None:
    """Write the files behind the have-list again, remove those p4 deleted that are back, and clear p4's leftovers;
    with dry_run, only list them."""
    behind = put.resync + put.remove
    if behind:
        _log_paths('Files behind the have-list to resync' if dry_run else 'Resyncing the files behind the have-list',
                   behind)
        if not dry_run:
            p4_sync_to_have(_local_paths(
                put.resync, workspace_dir), workspace_dir)
            _remove(put.remove, workspace_dir)
            log.success(f'{plural(len(behind), "file")} resynced')
    if put.leftovers:
        _log_paths(
            "p4's leftovers to clear" if dry_run else "Clearing p4's leftovers", put.leftovers)
        if not dry_run:
            _remove(put.leftovers, workspace_dir)
            log.success(f'{plural(len(put.leftovers), "file")} cleared')


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
        raise CommandError(f'{", ".join(given)} would change the plan of the interrupted sync, which a resume runs as '
                           f'it was made. Run "git p4son sync" without {"it" if len(given) == 1 else "them"} to '
                           'finish the interrupted sync.')


def _resume_sync(args: argparse.Namespace, progress: SyncProgress) -> int:
    """Finish the sync progress holds: check where it left the sync branch and its files, run the pre-sync hooks,
    then the passes left and the end."""
    workspace_dir = args.workspace_dir
    backend = args.backend
    # [start] and the sync branch's shape are the backend's, as much a part of the plan as its passes.
    recorded = progress.start['backend']
    if recorded != backend.words.tool:
        raise CommandError(f'The interrupted sync ran with the {recorded} backend. Run "git p4son --backend '
                           f'{recorded} sync" to finish it.')
    sync_revision = backend.resume_sync(
        progress.sync_branch, progress.tip, progress.start)
    passes = progress.passes
    left = (f'{plural(len(passes), "changelist")} left, to CL {passes[-1].changelist}' if passes
            else 'only the merge-back is left')
    log.heading(f'Resuming the interrupted sync: {left}')
    log.success(
        f'Started {progress.started:%Y-%m-%d %H:%M} UTC by git-p4son {progress.written_by}')

    depot_root = progress.depot_root
    check_no_blocking_processes(workspace_dir, args.ignore_blocking_processes)
    _check_p4_workspace_clean(depot_root, workspace_dir, sync_revision)
    put = _sort_files(progress, backend, workspace_dir)
    if args.dry_run:
        _put_right(put, workspace_dir, dry_run=True)
        log.heading('Where sync returns to')
        log.success(backend.returns_to() or f'Stays on {progress.sync_branch}')
        return 0

    last_synced = sync_state(
        workspace_dir, progress.pre_sync_commit).last_synced
    _run_pre_sync_hooks(last_synced, depot_root,
                        workspace_dir, backend.words.tool)
    # The client spec's line endings and clobber option, with the depot root as the interrupted sync planned it.
    client = sync_settings(workspace_dir)
    settings = dataclasses.replace(client, depot_root=depot_root)

    def run() -> SyncState:
        backend.begin_resume()
        _put_right(put, workspace_dir)
        resume_sync_passes(progress, settings, workspace_dir, backend)
        return sync_state(workspace_dir)

    return _finish_on_branch(OnSyncBranch.from_args(args, sync_revision), depot_root, last_synced, run)


def sync_command(args: argparse.Namespace) -> int:
    """Execute the sync command."""
    workspace_dir = args.workspace_dir
    check_hooks(workspace_dir)
    progress = read_sync_progress(workspace_dir)
    if progress is not None:
        _check_no_plan_options(args)
        return _resume_sync(args, progress)
    if args.force:
        log.warning('--force is deprecated, use --allow-backwards instead')
        args.allow_backwards = True
    backend = args.backend
    sync_revision = backend.prepare_sync(args.branch)
    plan = _plan_sync(args, sync_revision)
    if plan is None:
        return 0

    if args.dry_run:
        _add_split_targets(plan, workspace_dir)
        _log_sequence(plan)
        return 0

    def split() -> None:
        _add_split_targets(plan, workspace_dir)
        if plan.split:
            _log_sequence(plan)

    def run() -> SyncState:
        # A sync forward catches up to the last sync alone: its targets decide the cherry-picked files' revisions.
        catch_up = catch_up_passes(
            plan.state if plan.resync_last_synced else SyncState(plan.last_synced))
        run_sync_passes(catch_up, [SyncPass(cl, label) for cl, label in plan.targets], plan.settings,
                        workspace_dir, backend, record_progress=True)
        return SyncState(plan.targets[-1][0]) if plan.targets else plan.state

    return sync_on_branch(OnSyncBranch.from_args(args, sync_revision),
                          plan.settings.depot_root, plan.last_synced, run, split)


def _add_split_targets(plan: _SyncPlan, workspace_dir: str) -> None:
    """Add the targets that give each split user's changelist a commit of its own, when plan splits."""
    # _should_split only splits after a sync, so last_synced is set.
    if plan.split and plan.last_synced is not None:
        log.heading('Finding split users')
        users = resolve_split_users(plan.split_users, workspace_dir)
        log.success(', '.join(users))
        plan.targets = _split_targets(plan.targets, users, plan.last_synced,
                                      plan.settings.depot_root, workspace_dir)


def _log_sequence(plan: _SyncPlan) -> None:
    log.heading('Sync sequence')
    if plan.resync_last_synced:
        log.success(f'{plan.last_synced} ({LAST_SYNCED_LABEL})')
    else:
        log.success(' '.join(str(cl) for cl, _ in plan.targets))
