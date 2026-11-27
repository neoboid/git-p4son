"""The sync command: plans a sync from its arguments, or hands an interrupted one to its resume, and runs it."""

import argparse
from dataclasses import dataclass

from .common import CommandError
from .git import SyncState
from .hooks import check_hooks
from .log import log, plural
from .sync_progress import read_sync_progress
from .sync_targets import default_sync_target, resolve_sync_target
from .sync_split_users import canonical_split_users, get_split_users, resolve_split_users
from .perforce import get_latest_changelist, get_latest_changes, get_user_changes
from .sync_passes import (
    catch_up_passes, find_sync_state, LAST_SYNCED_LABEL, OnSyncBranch, run_sync_passes, sync_on_branch,
    sync_settings, SyncPass, SyncSettings,
)
from .sync_resume import resume_interrupted_sync


_SPLIT_LABEL = 'split'
_SPECIFIED_LABEL = 'specified'


def _check_increasing(changelists: list[tuple[int, str]]) -> None:
    """Raise CommandError unless the changelists, each with how the error names it, are strictly increasing."""
    for (prev, prev_named), (curr, curr_named) in zip(changelists, changelists[1:]):
        if curr <= prev:
            raise CommandError('Changelists must be strictly increasing, '
                               f'got {prev_named} then {curr_named}')


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
        log.warning('Not splitting out changelists: '
                    'no previous sync to start from')
        return False
    if targets[0][0] < last_synced:
        log.warning('Not splitting out changelists: '
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

    log.heading('Checking sync targets')
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
        log.warning(f'Skipping CL {last_synced} (already synced)')
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
            log.success(f'Already at {state}, nothing to sync')
            return None
        final = targets[-1][0]
        taken_back = [
            changelist for changelist in state.cherry_picks if changelist > final]
        if taken_back:
            log.warning(f'Syncing to CL {final} takes back the cherry-picks of CL '
                        f'{", ".join(map(str, taken_back))}')

    split = _should_split(split_users, last_synced, targets)
    if targets:
        log.success(f'To CL {targets[-1][0]}')
    return _SyncPlan(
        settings=settings, state=state, targets=targets,
        resync_last_synced=resync_last_synced, split_users=split_users, split=split)


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


def sync_command(args: argparse.Namespace) -> int:
    """Execute the sync command."""
    workspace_dir = args.workspace_dir
    check_hooks(workspace_dir)
    progress = read_sync_progress(workspace_dir)
    if progress is None and args.abort:
        raise CommandError('There is no interrupted sync to abort.')
    if progress is not None:
        return resume_interrupted_sync(args, progress)
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
