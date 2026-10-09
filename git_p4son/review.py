"""Review command: creates or updates a changelist per commit through an interactive rebase."""

import argparse
import shlex
from .changelist_store import alias_exists, validate_alias_name
from .git import get_commit_lines_since, resolve_editor
from .log import log
from .rebase_todo import pick_line, run_todo_rebase


def _generate_todo(commit_lines: list[str], alias: str, message: str,
                   force: bool, message_file: str | None = None,
                   no_commit_list: bool = False) -> str:
    """Generate the rebase todo content with exec lines."""
    lines = []
    last_index = len(commit_lines) - 1
    for i, commit_line in enumerate(commit_lines):
        lines.append(pick_line(commit_line))

        if i == 0:
            # First commit: create new changelist with review
            if message_file:
                message_arg = f'-F {shlex.quote(message_file)}'
            else:
                message_arg = f'-m {shlex.quote(message)}'
            cmd = f'new {shlex.quote(alias)} --review {message_arg}'
            if force:
                cmd += ' --force'
        else:
            # Subsequent commits: update and shelve
            cmd = f'update {shlex.quote(alias)} --shelve'

        if no_commit_list:
            cmd += ' --no-commit-list'

        # Sleep after all exec lines except the last
        if i < last_index:
            cmd += ' --sleep 5'
        lines.append(f'exec git p4son {cmd}')

    return '\n'.join(lines) + '\n'


def review_command(args: argparse.Namespace) -> int:
    """Execute the review command."""
    workspace_dir = args.workspace_dir

    # The generated rebase todo is line-based, so an embedded newline in
    # the message would split the exec line and break the rebase. A message
    # read from a file is passed on by file name instead.
    if not args.file and args.message and '\n' in args.message:
        log.error(
            'Review message must be a single line (use -F for a multi-line message)')
        return 1

    # Validate alias name before starting
    log.heading(f'Validating alias "{args.alias}"')
    error = validate_alias_name(args.alias)
    if error:
        log.error(error)
        return 1
    log.success('Done')

    # Check alias availability before starting
    if not args.force:
        log.heading(f'Checking alias "{args.alias}" is available')
        if alias_exists(args.alias, workspace_dir):
            log.error(
                f'Alias "{args.alias}" already exists '
                f'(use -f/--force to overwrite)')
            return 1
        log.success('Done')

    # Validate editor is available before starting
    if not args.no_edit_todo:
        log.heading('Checking editor')
        editor = resolve_editor(workspace_dir)
        if not editor:
            log.error(
                'No git editor configured. Set one with: git config core.editor <editor>')
            return 1
        log.success(editor)

    # Get commits since base branch
    log.heading('Finding commits')
    commit_lines = get_commit_lines_since(args.base_branch, workspace_dir)

    if not commit_lines:
        log.error(f'No commits found since {args.base_branch}')
        return 1
    log.success(f'{len(commit_lines)} commits since {args.base_branch}')

    # Generate the rebase todo
    log.heading('Generating rebase todo')
    todo_content = _generate_todo(
        commit_lines, args.alias, args.message, args.force, args.file,
        args.no_commit_list)

    if args.dry_run:
        log.info('Generated rebase todo:')
        log.info(todo_content)
        return 0

    return run_todo_rebase(todo_content, args.base_branch, workspace_dir,
                           edit_todo=not args.no_edit_todo)
