"""Review command: creates or updates a changelist per commit through an interactive rebase."""

import argparse
import shlex
from .changelist_store import check_alias_available, validate_alias_name
from .log import log
from .rebase_todo import (
    MESSAGE_FILE, build_todo, find_commits, run_todo_rebase,
)


def _generate_todo(commit_lines: list[str], alias: str, force: bool,
                   no_commit_list: bool = False) -> str:
    """Generate the rebase todo content with exec lines."""
    new_cmd = (f'new {shlex.quote(alias)} --review '
               f'-F {shlex.quote(MESSAGE_FILE)}')
    if force:
        new_cmd += ' --force'
    update_cmd = f'update {shlex.quote(alias)} --shelve'
    commands = [new_cmd] + [update_cmd] * (len(commit_lines) - 1)
    if no_commit_list:
        commands = [cmd + ' --no-commit-list' for cmd in commands]
    return build_todo(commit_lines, commands)


def review_command(args: argparse.Namespace) -> int:
    """Execute the review command."""
    workspace_dir = args.workspace_dir

    log.heading(f'Validating alias "{args.alias}"')
    error = validate_alias_name(args.alias)
    if error:
        log.error(error)
        return 1
    log.success('Done')

    if not args.force:
        log.heading(f'Checking alias "{args.alias}" is available')
        if not check_alias_available(args.alias, workspace_dir):
            return 1
        log.success('Done')

    commit_lines = find_commits(args.base_branch, workspace_dir)
    if commit_lines is None:
        return 1

    log.heading('Generating rebase todo')
    todo_content = _generate_todo(
        commit_lines, args.alias, args.force,
        no_commit_list=args.no_commit_list)

    return run_todo_rebase(todo_content, args.base_branch, workspace_dir,
                           edit_todo=not args.no_edit_todo,
                           message=args.message, dry_run=args.dry_run)
