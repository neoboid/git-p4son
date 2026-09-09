"""
Sync-split command implementation for git-p4son.
"""

import argparse

from .log import log
from .perforce import (
    P4Change,
    get_latest_changelist,
    get_p4_user,
    get_submitted_changes,
)
from .sync import (
    git_last_sync, resolve_depot_root, sync_command, sync_preflight,
)


def build_sync_targets(changes: list[P4Change], users: list[str],
                       last_synced: int, upper: int) -> list[int]:
    """Build the sync sequence that splits out the given users' changelists.

    changes is every submitted changelist affecting the depot root in
    [last_synced, upper], oldest first. Each changelist submitted by one of
    users gets the changelist submitted just before it synced first, so that
    submit lands in a commit containing nothing else. Changelists at or below
    last_synced are already in git and dropped, and the sequence always ends
    at upper.
    """
    targets: list[int] = []
    lowered = {u.lower() for u in users}
    for i, change in enumerate(changes):
        if change.change <= last_synced or change.user.lower() not in lowered:
            continue
        last = targets[-1] if targets else last_synced
        if i > 0 and changes[i - 1].change > last:
            targets.append(changes[i - 1].change)
            last = targets[-1]
        if change.change > last:
            targets.append(change.change)
    if not targets or upper > targets[-1]:
        targets.append(upper)
    return targets


def _resolve_users(args: argparse.Namespace, workspace_dir: str) -> list[str]:
    """Resolve the users whose changelists to split out, or [] on failure.

    Defaults to the current Perforce user when no --user was given. Repeated
    names are collapsed, keeping the order they were given in.
    """
    log.heading('Finding Perforce users')
    given = args.user or []
    if not given:
        current = get_p4_user(workspace_dir)
        if not current:
            log.error('Cannot determine the current Perforce user. '
                      'Pass --user NAME.')
            return []
        given = [current]

    users: list[str] = []
    seen: set[str] = set()
    for user in given:
        if user.lower() not in seen:
            seen.add(user.lower())
            users.append(user)
    log.success(', '.join(users))
    return users


def sync_split_command(args: argparse.Namespace) -> int:
    """Execute the sync-split command."""
    workspace_dir = args.workspace_dir
    invocation_dir = vars(args).get('invocation_dir', workspace_dir)

    resolved = resolve_depot_root(workspace_dir)
    if resolved is None:
        return 1
    depot_root = resolved.depot_root

    log.heading('Finding last synced changelist')
    last_sync = git_last_sync(workspace_dir)
    if not last_sync:
        log.error('No previous sync found. Run "git p4son sync" once to '
                  'establish a starting point.')
        return 1
    log.success(f'CL {last_sync.changelist}')
    last_synced = last_sync.changelist

    if args.changelist is None or args.changelist.lower() == 'head':
        log.heading('Finding latest changelist')
        upper = get_latest_changelist(depot_root, workspace_dir)
        log.success(f'CL {upper}')
    else:
        try:
            upper = int(args.changelist)
        except ValueError:
            log.error(f'Invalid changelist number: {args.changelist}')
            return 1

    # sync-split only ever moves forward from the last synced changelist: the
    # range it scans for the users' submits starts there.
    if upper == last_synced:
        log.info('Already synced, nothing to do.')
        return 0
    if upper < last_synced:
        log.error(f'Cannot sync back to CL {upper} '
                  f'(currently at CL {last_synced}). '
                  'Use "git p4son sync --force" to sync to an older '
                  'changelist.')
        return 1

    # The workspace checks and pre-sync hooks run here rather than being left
    # to sync_command, which only reaches them after all of the resolution
    # below. That resolution costs several p4 round trips, so a dirty
    # workspace or a hook vetoing the sync should get to say so first.
    # sync_command is told it is done so none of it happens twice, and a dry
    # run syncs nothing, so it skips the gate entirely.
    if not args.dry_run:
        if not sync_preflight(depot_root, workspace_dir, invocation_dir):
            return 1
        args.preflight_done = True

    users = _resolve_users(args, workspace_dir)
    if not users:
        return 1

    # One query covers both jobs: which changelists in the range belong to
    # the given users, and which changelist was submitted immediately before
    # each of them (its predecessor in this same list).
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
        log.success('None found, syncing to the target changelist only')

    targets = build_sync_targets(changes, users, last_synced, upper)

    log.heading('Sync sequence')
    log.success(' '.join(str(cl) for cl in targets))

    if args.dry_run:
        log.info(f'Would run: git p4son sync '
                 f'{" ".join(str(cl) for cl in targets)}')
        return 0

    args.changelist = [str(cl) for cl in targets]
    args.force = False
    return sync_command(args)
