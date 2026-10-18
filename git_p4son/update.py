"""Update command: updates an existing changelist from the git change."""

import argparse
import shlex
from dataclasses import replace
from .changelist_store import find_record, load_changelist_alias, update_record
from .common import CommandError
from .git import get_head_subject, resolve_commit
from .included import Plan, catch_up_subject, included_count, plan_update
from .lib import edit_and_shelve, update_changelist
from .log import log, plural
from .per_commit import SWARM_SHELF_DELAY, message_args, run_per_commit


def _step(changelist: str, args: argparse.Namespace) -> list[str]:
    """The update step for every commit; run_per_commit gives the first one the message."""
    flags = [flag for flag, on in (
        ('--no-commit-list', args.no_commit_list),
        ('--no-edit', args.no_edit),
        ('--shelve', args.shelve)) if on]
    return ['update', changelist, *flags]


def _update_per_commit(changelist: str, args: argparse.Namespace) -> None:
    """Update the changelist once per commit since the base branch."""
    # Steps name the changelist by number, as resolved once here.
    run_per_commit(_step(changelist, args), None, args.base_branch, args.workspace_dir, args.backend,
                   sleep=SWARM_SHELF_DELAY if args.shelve else None, dry_run=args.dry_run,
                   message=args.message, message_file=args.file)


def _check_per_commit(plan: Plan, changelist: str, no_edit: bool, given: bool, workspace_dir: str) -> None:
    """Raise CommandError unless the per-commit loop would start right after the commits the CL includes."""
    if no_edit:
        raise CommandError(f'--no-edit opens no files, so CL {changelist} can\'t include its new commits, and '
                           '--per-commit has nothing to do. Leave out --no-edit, or --per-commit to change only the '
                           'description.')
    # The resume hint after a failed step names the last commit that worked, which is where the loop starts.
    if given and resolve_commit(plan.start, workspace_dir) != plan.after:
        raise CommandError(f'--per-commit runs on the commits CL {changelist} doesn\'t include yet, the ones after '
                           f'commit {included_count(plan.kept)} since {plan.current.base}, so -b would shelve '
                           'included ones again. Leave out -b.')


def _check_anything_new(plan: Plan, changelist: str) -> None:
    """Raise CommandError if the per-commit loop would have no commit to run on."""
    if not plan.subjects:
        raise CommandError(f'CL {changelist} already includes the {plural(included_count(plan.kept), "commit")} '
                           f'since {plan.current.base}.')


def _warn_dropped(plan: Plan, changelist: str) -> None:
    """Warn about the commits the CL included that are gone from the branch, whose patches stay in the review."""
    if not plan.dropped:
        return
    positions = ', '.join(str(position) for position in plan.dropped)
    log.warning(f'Gone from the branch, dropped or squashed into another, of the '
                f'{plural(included_count(plan.current), "commit")} CL {changelist} included: number {positions}. '
                'The review keeps their patches. To undo a shelved commit, revert it in a new commit rather than '
                'dropping it.')


def _catch_up(plan: Plan, args: argparse.Namespace, workspace_dir: str) -> bool:
    """Insert the empty commit that gives a moved base its own patch, if the CL's shelf needs one; True if it did.

    A dry run only says what it would insert. The record then has the moved base, so the steps of a loop that shelves
    don't catch up again, and the catch-up, so a rerun after a failed shelve still shelves it alone."""
    subject = catch_up_subject(plan, workspace_dir, args.backend.revision)
    if subject is None:
        return False
    after = get_head_subject(workspace_dir, plan.after)
    if args.dry_run:
        log.info(
            f'Would insert "{subject}" after "{after}", as the base moved since the last shelve.')
        return False
    log.heading('Catching up with the base')
    commit: str = args.backend.insert_empty_after(plan.after, subject)
    update_record(plan.alias, replace(plan.kept, base_commit=plan.next.base_commit,
                                      catch_up=args.backend.commit_identities([commit])[0]), workspace_dir)
    log.success(f'Inserted "{subject}" after "{after}"')
    return True


def _shelve_twice(plan: Plan, catch_up: str, changelist: str, args: argparse.Namespace, workspace_dir: str) -> None:
    """Shelve at the catch-up, then at the tip, so the catch-up is a patch of its own, before the new commits'."""
    rerun = shlex.join(['git', 'p4son', *_step(changelist, args),
                       *message_args(args.message, args.file)])
    try:
        run_per_commit(_step(changelist, args), None, plan.after, workspace_dir, args.backend,
                       sleep=SWARM_SHELF_DELAY, dry_run=False, message=args.message, message_file=args.file,
                       resumable=lambda: None,
                       only={catch_up, resolve_commit(args.backend.revision, workspace_dir)})
    except CommandError as error:
        # The record keeps the catch-up until it is included, so the same command finishes either step.
        raise CommandError(
            f'{error}\nAfter fixing the problem, run "{rerun}" again.', error.returncode) from error


def _new_commits(plan: Plan | None, no_edit: bool) -> list[str] | None:
    """The subjects to append, none with --no-edit, or None to merge by subject without a record."""
    if plan is None:
        return None
    return [] if no_edit else plan.subjects


def update_command(args: argparse.Namespace) -> int:
    """Execute the update command."""
    workspace_dir = args.workspace_dir

    if args.changelist.isdigit():
        changelist = args.changelist
    else:
        log.heading('Resolving alias')
        changelist = load_changelist_alias(args.changelist, workspace_dir)
        log.success(f'{args.changelist} -> CL {changelist}')

    revision = args.backend.revision
    found = find_record(changelist, workspace_dir)
    plan = None
    if found is not None:
        # As typed for messages, and resolved by the backend, as a jj revision means nothing to git.
        given = None if args.base_given is None else (
            args.base_given, args.base_branch)
        plan = plan_update(found[1], found[0], changelist,
                           given, args.backend, workspace_dir, revision)
        _warn_dropped(plan, changelist)
        if args.per_commit:
            # Recorded now, so neither the steps, each planning on its own, nor a refused run warns about them again.
            if plan.dropped and not args.dry_run:
                update_record(plan.alias, plan.kept, workspace_dir)
            _check_per_commit(plan, changelist, args.no_edit,
                              given is not None, workspace_dir)
        if args.shelve and not args.no_edit:
            # Checked before the catch-up rewrites the branch, as checkouts and opens would refuse it after.
            args.backend.check_clean()
            if _catch_up(plan, args, workspace_dir):
                revision = args.backend.revision
                alias, record = find_record(changelist, workspace_dir) or found
                plan = plan_update(record, alias, changelist,
                                   given, args.backend, workspace_dir, revision)
            if plan.catch_up is not None and not args.per_commit and len(plan.subjects) > 1:
                _shelve_twice(plan, plan.catch_up,
                              changelist, args, workspace_dir)
                return 0
        if args.per_commit:
            _check_anything_new(plan, changelist)
        args.base_branch = plan.start

    if args.per_commit:
        _update_per_commit(changelist, args)
        return 0

    # Opening and reverting files relies on every tracked file matching the change.
    if not args.no_edit:
        args.backend.check_clean()

    if args.message is not None or not args.no_commit_list:
        log.heading(f'Updating description for CL {changelist}')
        update_changelist(
            changelist, args.base_branch, workspace_dir, dry_run=args.dry_run,
            message=args.message, commit_list=not args.no_commit_list,
            revision=revision, new_commits=_new_commits(plan, args.no_edit))
        log.success(f'Would update CL {changelist} with this description'
                    if args.dry_run else f'Updated CL {changelist}')

    edit_and_shelve(changelist, args.base_branch, workspace_dir,
                    edit=not args.no_edit, shelve=args.shelve,
                    dry_run=args.dry_run, revision=revision)
    # Without their files opened, the new commits stay uncounted, for the next update to include.
    if plan is not None and not args.dry_run and not args.no_edit:
        # base_commit is the base of the last shelve, which a catch-up is measured from.
        shelved = plan.next if args.shelve else replace(
            plan.next, base_commit=plan.current.base_commit)
        update_record(plan.alias, shelved, workspace_dir)
    return 0
