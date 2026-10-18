"""Update command: updates an existing changelist from the git change."""

import argparse
from .changelist_store import find_record, load_changelist_alias, update_record
from .common import CommandError
from .git import resolve_commit
from .included import Plan, plan_update
from .lib import edit_and_shelve, update_changelist
from .log import log, plural
from .per_commit import SWARM_SHELF_DELAY, run_per_commit


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


def _check_per_commit(plan: Plan, changelist: str, no_edit: bool, workspace_dir: str) -> None:
    """Raise CommandError unless the per-commit loop would run on exactly the commits the CL doesn't include yet."""
    current = plan.current
    if no_edit:
        raise CommandError(f'--no-edit opens no files, so CL {changelist} can\'t include its new commits, and '
                           '--per-commit has nothing to do. Leave out --no-edit, or --per-commit to change only the '
                           'description.')
    if not plan.subjects:
        raise CommandError(f'CL {changelist} already includes the {plural(current.included, "commit")} '
                           f'since {current.base}.')
    # The resume hint after a failed step names the last commit that worked, which is where the loop starts.
    if resolve_commit(plan.start, workspace_dir) != plan.after:
        raise CommandError(f'--per-commit runs on the commits CL {changelist} doesn\'t include yet, the ones after '
                           f'commit {current.included} since {current.base}, so -b would shelve included ones again. '
                           'Leave out -b.')


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
        args.base_branch = plan.start

    if args.per_commit:
        if plan is not None:
            _check_per_commit(plan, changelist, args.no_edit, workspace_dir)
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
        update_record(plan.alias, plan.next, workspace_dir)
    return 0
