"""Sync command implementation for git-p4son."""

import argparse
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

from .backend import Backend
from .common import CommandError, RunError
from .git import (
    SyncState, files_changed_outside_syncs, format_cherry_pick_subject, format_sync_subject, get_head_commit,
    get_tracked_files, sync_state,
)
from .hooks import check_hooks, run_hook
from .depot import resolve_depot_root
from .log import log, plural
from .processes import check_no_blocking_processes
from .sync_targets import default_sync_target, resolve_sync_target
from .sync_merge import (
    WritableSyncFileSet, merge_changed_files, prepare_writable_files,
    report_not_synced,
)
from .writable import check_not_allwrite, is_writable_mode, restore_writable
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
    p4_sync,
    p4_sync_preview,
    P4SyncAbortError,
    P4SyncClobberError,
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
        _check_refused(e.refused, expected_refusals, e.returncode)
    except P4SyncAbortError as e:
        log.info(str(e))
        raise CommandError(
            'Aborting sync: p4 could not replace a file, most likely because '
            'another process has it open. The workspace is left partially '
            'synced: close that process, then commit or restore the synced '
            'files before syncing again.') from e


def _check_refused(refused: list[str], expected: set[str], returncode: int) -> None:
    """Raise CommandError, after listing them, if p4 refused files not in expected."""
    unexpected = [f for f in refused if f not in expected]
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
    """The workspace's depot root and the client spec's line endings and clobber option.

    Raises CommandError for a client spec with the allwrite option."""
    resolved = resolve_depot_root(workspace_dir)
    client_spec = resolved.client_spec
    check_not_allwrite(client_spec)
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
    if preview:
        _sync_changelist(sync_pass, settings.depot_root, ctx.workspace_dir,
                         expected_refusals=set(prep.ignored))
        prep.synced = [entry.filepath for entry in preview]
    return prep


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


def run_sync_passes(catch_up: list[SyncPass], targets: list[SyncPass], settings: SyncSettings, workspace_dir: str,
                    backend: Backend) -> None:
    """Sync catch_up, folded into the first target's commit, then commit each target; merge local changes."""
    log.heading('Finding HEAD commit')
    pre_sync_head_commit = get_head_commit(workspace_dir)
    log.success(f'{pre_sync_head_commit}')

    with tempfile.TemporaryDirectory(prefix='git-p4son-sync-') as temp_root:
        ctx = _SyncContext(workspace_dir, settings, pre_sync_head_commit, temp_root,
                           files_changed_outside_syncs(workspace_dir, pre_sync_head_commit))

        preps = [_sync_pass(sync_pass, ctx) for sync_pass in catch_up]

        # Local changes are merged back once at the end, so each commit is pure Perforce state.
        for target in targets:
            preps.append(_sync_pass(target, ctx))
            if target.exact and not preps[-1].synced:
                log.warning(
                    f'CL {target.changelist} changes no files in the workspace')

            log.heading(f'Committing git changes for CL {target.changelist}')
            staged = backend.commit_sync(target.subject(settings.depot_root))
            log.success(f'Committed {plural(staged, "file")}')

        # Dedup files that showed up in several sync passes.
        by_path = {cf.filepath: cf for p in preps for cf in p.changed}
        changed_files = sorted(by_path.values(), key=lambda cf: cf.filepath)
        merge_changed_files(changed_files, workspace_dir, temp_root)

        report_not_synced(preps, settings.clobber_mode, workspace_dir)

        _make_synced_files_writable(
            [f for p in preps for f in p.synced], workspace_dir)


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
    try:
        state = run()
        exit_code = _run_post_sync_hooks(
            state.latest, last_synced, depot_root, on.workspace_dir, backend.words.tool)
    except BaseException:
        backend.fail_sync()
        raise
    backend.finish_sync(str(state), on.rebase)
    return exit_code


def sync_command(args: argparse.Namespace) -> int:
    """Execute the sync command."""
    workspace_dir = args.workspace_dir
    check_hooks(workspace_dir)
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
                        workspace_dir, backend)
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
