"""Review command: creates a changelist and updates it once per commit, one shelf each."""

import argparse
from .changelist_store import check_alias_usable, list_changelist_aliases
from .log import log
from .per_commit import SWARM_SHELF_DELAY, run_per_commit


def _steps(args: argparse.Namespace) -> tuple[list[str], list[str]]:
    """The new step for the first commit and the update step for the others."""
    commit_list = ['--no-commit-list'] if args.no_commit_list else []
    force = ['--force'] if args.force else []
    return (['new', args.alias, '--review', *force, *commit_list],
            ['update', args.alias, '--shelve', *commit_list])


def review_command(args: argparse.Namespace) -> int:
    """Execute the review command."""
    workspace_dir = args.workspace_dir

    log.heading(f'Checking alias "{args.alias}"')
    check_alias_usable(args.alias, workspace_dir, args.force)
    log.success(f'Alias "{args.alias}" is usable')

    aliases_before = dict(list_changelist_aliases(workspace_dir))

    def resumable() -> str | None:
        # Once the first step has saved the alias, its changelist exists.
        changelist = dict(list_changelist_aliases(
            workspace_dir)).get(args.alias)
        if changelist is None or changelist == aliases_before.get(args.alias):
            return None
        return f'CL {changelist} exists.'

    first, rest = _steps(args)
    run_per_commit(first, rest, args.base_branch, workspace_dir, args.backend,
                   sleep=SWARM_SHELF_DELAY, dry_run=args.dry_run,
                   message=args.message, message_file=args.file, resumable=resumable)
    return 0
