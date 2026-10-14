"""Review command: creates or updates a changelist per commit through an interactive rebase."""

import argparse
import shlex
from .changelist_store import check_alias_usable
from .log import log
from .rebase_todo import SWARM_SHELF_DELAY, rebase_per_commit


def _step_commands(alias: str, force: bool,
                   no_commit_list: bool) -> tuple[str, str]:
    """The new command for the first commit and the update command for the others."""
    new_cmd = f'new {shlex.quote(alias)} --review'
    if force:
        new_cmd += ' --force'
    update_cmd = f'update {shlex.quote(alias)} --shelve'
    if no_commit_list:
        new_cmd += ' --no-commit-list'
        update_cmd += ' --no-commit-list'
    return new_cmd, update_cmd


def review_command(args: argparse.Namespace) -> int:
    """Execute the review command."""
    workspace_dir = args.workspace_dir

    log.heading(f'Checking alias "{args.alias}"')
    if not check_alias_usable(args.alias, workspace_dir, args.force):
        return 1
    log.success('Done')

    new_cmd, update_cmd = _step_commands(args.alias, args.force,
                                         args.no_commit_list)
    return rebase_per_commit(new_cmd, update_cmd, args.base_branch,
                             workspace_dir, message=args.message,
                             sleep=SWARM_SHELF_DELAY,
                             edit_todo=not args.no_edit_todo,
                             dry_run=args.dry_run)
