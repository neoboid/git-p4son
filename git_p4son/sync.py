"""Sync command implementation for git-p4son."""

import argparse
import tempfile
from dataclasses import dataclass

from .common import CommandError
from .git import (
    add_all_files, commit, get_head_commit, get_staged_files, get_tracked_files,
    format_sync_subject, git_last_synced_changelist,
)
from .hooks import run_hooks
from .lib import check_git_workspace_clean
from .depot import resolve_depot_root
from .log import log, plural
from .processes import check_no_blocking_processes
from .sync_merge import (
    WritableSyncFileSet, merge_changed_files, prepare_writable_files,
    report_not_synced,
)
from .writable import is_writable_mode, restore_writable
from .sync_split_users import (
    canonical_split_users,
    get_split_users,
    resolve_split_users,
)
from .perforce import (
    ClobberMode,
    get_latest_changelist,
    get_submitted_changes,
    p4_get_opened_files,
    p4_sync,
    p4_sync_preview,
    P4SyncAbortError,
    P4SyncClobberError,
    P4Change,
)


LAST_SYNCED_LABEL = 'last synced'
_SPLIT_LABEL = 'split'


def _sync_changelist(changelist: int, label: str, depot_root: str,
                     workspace_dir: str,
                     expected_clobber: set[str]) -> list[str]:
    """Sync to changelist, returning the expected_clobber files p4 refused.

    Any other refused file raises.
    """
    log.heading(f'Syncing to {label} CL ({changelist})')
    try:
        p4_sync(changelist, depot_root, workspace_dir)
        return []
    except P4SyncClobberError as e:
        unexpected = [f for f in e.refused if f not in expected_clobber]
        if unexpected:
            for f in unexpected:
                log.info(f)
            raise CommandError('Unexpected clobber errors for the files above',
                               returncode=e.returncode) from e
        log.warning(
            f'{plural(len(e.refused), "expected clobber error")} '
            '(git-ignored files)')
        return e.refused
    except P4SyncAbortError as e:
        log.info(str(e))
        raise CommandError(
            'Aborting sync: p4 could not replace a file, most likely because '
            'another process has it open. The workspace is left partially '
            'synced: close that process, then commit or restore the synced '
            'files before syncing again.') from e


def _check_p4_workspace_clean(depot_root: str, workspace_dir: str) -> None:
    """Raise CommandError, after listing them, if git-tracked files are opened in Perforce."""
    log.heading('Checking files opened in Perforce')
    opened_files = p4_get_opened_files(depot_root, workspace_dir)
    tracked = get_tracked_files([f for f, _ in opened_files], workspace_dir)
    tracked_opened_files = [(f, change) for f, change in opened_files
                            if f in tracked]
    if tracked_opened_files:
        for filename, change in tracked_opened_files:
            log.file_change(filename, change)
        raise CommandError('Workspace has p4-opened files tracked by git')
    if opened_files:
        log.warning(
            f'Ignoring {plural(len(opened_files), "p4-opened file")} '
            'not tracked by git')
    else:
        log.success('Clean')


def _run_pre_sync_hooks(workspace_dir: str, invocation_dir: str) -> None:
    """Run all pre-sync hooks; raise CommandError if any of them failed."""
    results = run_hooks('pre-sync', workspace_dir, invocation_dir)
    if any(result.returncode != 0 for result in results):
        raise CommandError('Aborting sync because a pre-sync hook failed')


def sync_preflight(depot_root: str, workspace_dir: str, invocation_dir: str,
                   ignore_blocking_processes: bool) -> None:
    """Gate a sync on blocking processes, clean workspaces and pre-sync hooks; raise CommandError if one fails."""
    check_no_blocking_processes(workspace_dir, ignore_blocking_processes)
    check_git_workspace_clean(workspace_dir)
    _check_p4_workspace_clean(depot_root, workspace_dir)
    _run_pre_sync_hooks(workspace_dir, invocation_dir)


@dataclass(frozen=True)
class _SyncContext:
    """What every pass of one sync shares."""
    workspace_dir: str
    depot_root: str
    pre_sync_head_commit: str
    temp_root: str
    uses_crlf: bool
    clobber_mode: ClobberMode


def _sync_pass(changelist: int, label: str,
               ctx: _SyncContext) -> WritableSyncFileSet:
    """Run one sync pass: preview, prepare writable files, and p4 sync."""
    preview = p4_sync_preview(changelist, ctx.depot_root, ctx.workspace_dir)
    prep = prepare_writable_files(preview, ctx.workspace_dir,
                                  ctx.pre_sync_head_commit, ctx.temp_root,
                                  uses_crlf=ctx.uses_crlf,
                                  clobber_mode=ctx.clobber_mode)
    if preview:
        prep.not_synced = _sync_changelist(changelist, label, ctx.depot_root,
                                           ctx.workspace_dir,
                                           expected_clobber=set(prep.ignored))
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


def _split_user_changes(changes: list[P4Change], users: list[str],
                        last_synced: int) -> list[P4Change]:
    """The changes submitted by one of users after last_synced, matching names case-insensitively."""
    lowered = {u.lower() for u in users}
    return [c for c in changes
            if c.change > last_synced and c.user.lower() in lowered]


def build_sync_targets(changes: list[P4Change], users: list[str],
                       last_synced: int, required: list[int]) -> list[int]:
    """Build the sync sequence that splits out the given users' changelists.

    Each of their changelists is preceded by the one submitted just before it,
    so it lands in a commit of its own. changes is oldest first.
    """
    to_split = {c.change for c in _split_user_changes(
        changes, users, last_synced)}
    split: list[int] = []
    for i, change in enumerate(changes):
        if change.change not in to_split:
            continue
        last = split[-1] if split else last_synced
        if i > 0 and changes[i - 1].change > last:
            split.append(changes[i - 1].change)
            last = split[-1]
        if change.change > last:
            split.append(change.change)
    return sorted(set(split).union(required))


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
    log.heading(f'Finding changelists submitted to {depot_root} '
                f'in CL {last_synced}..{upper}')
    changes = get_submitted_changes(depot_root, last_synced, upper,
                                    workspace_dir)
    log.success(plural(len(changes), 'changelist'))

    matched = _split_user_changes(changes, users, last_synced)
    log.heading('Finding changelists to split into their own commits')
    if matched:
        for change in matched:
            log.info(f'CL {change.change} ({change.user})')
        log.success(f'{plural(len(matched), "changelist")} to split out')
    else:
        log.success('None found')

    labels = dict(targets)
    return [(cl, labels.get(cl, _SPLIT_LABEL))
            for cl in build_sync_targets(changes, users, last_synced,
                                         list(labels))]


def _latest_target(depot_root: str, workspace_dir: str) -> tuple[int, str]:
    """Look up the latest submitted changelist as a sync target."""
    log.heading('Finding latest changelist')
    latest = get_latest_changelist(depot_root, workspace_dir)
    log.success(f'CL {latest}')
    return (latest, 'latest')


def _resolve_sync_targets(
        raw: list[str], last_synced: int | None, depot_root: str,
        workspace_dir: str, force: bool) -> list[tuple[int, str]]:
    """Resolve the ordered (changelist, label) targets a sync will visit.

    Returns [] when already at the only target; raises CommandError for invalid arguments.
    """
    lowered = [c.lower() for c in raw]

    if 'head' in lowered and lowered.index('head') != len(lowered) - 1:
        raise CommandError('The "head" keyword must come last')

    targets = []
    for c in raw or ['head']:
        if c.lower() == 'head':
            targets.append(_latest_target(depot_root, workspace_dir))
        else:
            try:
                targets.append((int(c), 'specified'))
            except ValueError:
                raise CommandError(f'Invalid changelist number: {c}') from None

    numbers = [cl for cl, _ in targets]
    for prev, curr in zip(numbers, numbers[1:]):
        if curr <= prev:
            raise CommandError('Changelists must be strictly increasing, '
                               f'got {prev} then {curr}')

    if last_synced and numbers[0] < last_synced:
        if not force:
            raise CommandError(
                f'Cannot sync to CL {numbers[0]} '
                f'(currently at CL {last_synced}) without --force.')
        log.warning(
            f'Syncing to older CL {numbers[0]} '
            f'(currently at CL {last_synced}) with --force')

    if last_synced in numbers:
        log.info(f'Skipping CL {last_synced} (already synced)')
        targets = [(cl, lbl) for cl, lbl in targets
                   if cl != last_synced]

    return targets


@dataclass
class _SyncPlan:
    """A resolved sync: the targets to visit after the catch-up pass to last_synced."""
    depot_root: str
    last_synced: int | None
    targets: list[tuple[int, str]]
    # Re-sync only last_synced; targets is empty.
    resync_last_synced: bool
    split_users: list[str]
    split: bool
    uses_crlf: bool
    clobber_mode: ClobberMode


def _plan_sync(args: argparse.Namespace) -> _SyncPlan | None:
    """Resolve what to sync from the arguments and the workspace, or None when already synced.

    Raises CommandError for an invalid argument.
    """
    workspace_dir = args.workspace_dir

    resolved = resolve_depot_root(workspace_dir)
    depot_root = resolved.depot_root
    client_spec = resolved.client_spec

    log.heading(f'Finding {LAST_SYNCED_LABEL} changelist')
    last_synced = git_last_synced_changelist(workspace_dir)
    if last_synced:
        log.success(f'CL {last_synced}')
    else:
        log.warning('No previous sync found')

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
            args.changelist, last_synced, depot_root, workspace_dir, args.force)
        if not targets:
            log.info('Already synced, nothing to do.')
            log.heading('Skipping post-sync hooks')
            return None

    return _SyncPlan(
        depot_root=depot_root, last_synced=last_synced, targets=targets,
        resync_last_synced=resync_last_synced, split_users=split_users,
        split=_should_split(split_users, last_synced, targets),
        uses_crlf=bool(client_spec and client_spec.uses_crlf),
        clobber_mode=(client_spec.clobber_mode if client_spec
                      else ClobberMode.NOCLOBBER))


def _run_sync(plan: _SyncPlan, workspace_dir: str,
              invocation_dir: str) -> None:
    """Sync and commit each target, then merge local changes back and run the post-sync hooks."""
    log.heading('Finding HEAD commit')
    pre_sync_head_commit = get_head_commit(workspace_dir)
    log.success(f'{pre_sync_head_commit}')

    with tempfile.TemporaryDirectory(prefix='git-p4son-sync-') as temp_root:
        ctx = _SyncContext(workspace_dir, plan.depot_root, pre_sync_head_commit,
                           temp_root, plan.uses_crlf, plan.clobber_mode)

        preps: list[WritableSyncFileSet] = []
        # Catch-up pass to the last synced changelist, folded into the first commit.
        if plan.last_synced:
            preps.append(_sync_pass(plan.last_synced, LAST_SYNCED_LABEL, ctx))

        # Local changes are merged back once at the end, so each commit is pure Perforce state.
        for changelist, changelist_label in plan.targets:
            preps.append(_sync_pass(changelist, changelist_label, ctx))

            log.heading(f'Committing git changes for CL {changelist}')
            add_all_files(workspace_dir)
            staged = get_staged_files(workspace_dir)
            commit(format_sync_subject(plan.depot_root, changelist),
                   workspace_dir, allow_empty=True)
            log.success(f'Committed {plural(len(staged), "file")}')

        # Dedup files that showed up in several sync passes.
        by_path = {cf.filepath: cf for p in preps for cf in p.changed}
        changed_files = sorted(by_path.values(), key=lambda cf: cf.filepath)
        merge_changed_files(changed_files, workspace_dir, temp_root)

        report_not_synced(preps, plan.clobber_mode, workspace_dir)

        _make_synced_files_writable(
            [f for p in preps for f in p.synced], workspace_dir)

        run_hooks('post-sync', workspace_dir, invocation_dir)


def sync_command(args: argparse.Namespace) -> int:
    """Execute the sync command."""
    workspace_dir = args.workspace_dir

    plan = _plan_sync(args)
    if plan is None:
        return 0

    # A dry run skips the checks and hooks, which run before the costly split queries.
    if not args.dry_run:
        sync_preflight(plan.depot_root, workspace_dir, args.invocation_dir,
                       args.ignore_blocking_processes)

    if plan.split:
        log.heading('Finding split users')
        users = resolve_split_users(plan.split_users, workspace_dir)
        log.success(', '.join(users))
        plan.targets = _split_targets(plan.targets, users, plan.last_synced,
                                      plan.depot_root, workspace_dir)

    if plan.split or args.dry_run:
        log.heading('Sync sequence')
        if plan.resync_last_synced:
            log.success(f'{plan.last_synced} ({LAST_SYNCED_LABEL})')
        else:
            log.success(' '.join(str(cl) for cl, _ in plan.targets))
    if args.dry_run:
        log.info('Dry run, nothing synced.')
        return 0

    _run_sync(plan, workspace_dir, args.invocation_dir)
    return 0
