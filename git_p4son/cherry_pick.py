"""Cherry-pick command implementation: sync the files of submitted changelists alone onto the sync branch."""

import argparse
from dataclasses import dataclass

from .common import CommandError
from .git import SyncState
from .log import log
from .perforce import describe_changelist
from .sync import (
    OnSyncBranch, SyncPass, SyncSettings, catch_up_passes, find_sync_state, parse_changelist, run_sync_passes,
    sync_on_branch, sync_settings,
)


@dataclass(frozen=True)
class _CherryPickPlan:
    """The passes that bring the workspace to the sync branch's state, and the cherry-picks after them."""
    settings: SyncSettings
    catch_up: list[SyncPass]
    targets: list[SyncPass]
    after: SyncState


def _check_submitted(changelists: list[int], workspace_dir: str) -> None:
    """Raise CommandError for a changelist that doesn't exist or isn't submitted."""
    log.heading('Checking the changelists are submitted')
    for changelist in changelists:
        record, _ = describe_changelist(str(changelist), workspace_dir)
        if record is None:
            raise CommandError(f'CL {changelist} does not exist')
        if record.get('status') != 'submitted':
            raise CommandError(f'CL {changelist} is not submitted. Cherry-pick takes submitted changelists; unshelve '
                               'a shelved one with p4 instead.')
    log.success('Submitted')


def _plan_cherry_pick(args: argparse.Namespace, sync_revision: str) -> _CherryPickPlan:
    """Resolve the cherry-picks against the sync branch at sync_revision; raise CommandError for one going backwards."""
    workspace_dir = args.workspace_dir
    changelists = [parse_changelist(c) for c in args.changelist]
    for changelist in changelists:
        if changelists.count(changelist) > 1:
            raise CommandError(f'CL {changelist} is given twice')
    settings = sync_settings(workspace_dir)
    state = find_sync_state(workspace_dir, sync_revision)
    if not state.last_synced:
        raise CommandError(
            'No previous sync found. Sync first, then cherry-pick onto it.')
    # The last sync bounds a cherry-pick; before it, files would go back to revisions git already moved past.
    for changelist in changelists:
        if changelist <= state.last_synced:
            raise CommandError(f'Cannot cherry-pick CL {changelist}, at or before the last synced CL '
                               f'{state.last_synced}')
    _check_submitted(changelists, workspace_dir)
    newest = max(state.cherry_picks, default=0)
    for changelist in changelists:
        if changelist < newest:
            log.warning(f'CL {changelist} is older than the cherry-picked CL {newest}: a file both changed goes to its '
                        f'revision in CL {changelist}')
        newest = max(newest, changelist)
    targets = [SyncPass(cl, 'cherry-pick', exact=True) for cl in changelists]
    return _CherryPickPlan(settings, catch_up_passes(state), targets,
                           SyncState(state.last_synced, (*state.cherry_picks, *changelists)))


def cherry_pick_command(args: argparse.Namespace) -> int:
    """Execute the cherry-pick command."""
    backend = args.backend
    sync_revision = backend.prepare_sync(None)
    plan = _plan_cherry_pick(args, sync_revision)

    log.heading('Cherry-pick sequence')
    log.success(' '.join(str(target.changelist) for target in plan.targets))
    if args.dry_run:
        return 0

    def run() -> SyncState:
        run_sync_passes(plan.catch_up, plan.targets, plan.settings,
                        args.workspace_dir, args.invocation_dir, backend)
        return plan.after

    sync_on_branch(OnSyncBranch.from_args(args, sync_revision),
                   plan.settings.depot_root, run)
    return 0
