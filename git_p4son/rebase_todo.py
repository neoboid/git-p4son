"""Runs an interactive rebase with a generated todo that runs git p4son after each pick."""

import argparse
import os
import shlex
import subprocess
from . import CONFIG_DIR
from .config import ensure_config_dir
from .git import get_commit_lines_since, resolve_editor
from .lib import check_git_workspace_clean
from .log import log

# Relative to the workspace root, where git runs the todo's exec lines.
MESSAGE_FILE = f'{CONFIG_DIR}/reviews/message'


def _reviews_dir(workspace_dir: str) -> str:
    """Return the path to the directory holding the generated todo."""
    return os.path.join(workspace_dir, CONFIG_DIR, 'reviews')


def _todo_path(workspace_dir: str) -> str:
    """Return the path to the generated todo file."""
    return os.path.join(_reviews_dir(workspace_dir), 'todo')


def pick_line(commit_line: str) -> str:
    """Turn a '<hash> <subject>' commit line into a rebase pick line."""
    parts = commit_line.split(' ', 1)
    subject = parts[1] if len(parts) > 1 else ''
    return f'pick {parts[0]} {subject}'


def find_commits(base_branch: str, workspace_dir: str) -> list[str] | None:
    """Return the commit lines since base_branch, or None (logged) if there are none."""
    log.heading('Finding commits')
    commit_lines = get_commit_lines_since(base_branch, workspace_dir)
    if not commit_lines:
        log.error(f'No commits found since {base_branch}')
        return None
    log.success(f'{len(commit_lines)} commits since {base_branch}')
    return commit_lines


def build_todo(commit_lines: list[str], commands: list[str]) -> str:
    """Build a rebase todo that runs git p4son with each command after its commit."""
    lines = []
    for i, (commit_line, cmd) in enumerate(zip(commit_lines, commands)):
        # Give Swarm time to process each shelf before the next
        if i < len(commit_lines) - 1:
            cmd += ' --sleep 5'
        lines.append(pick_line(commit_line))
        lines.append(f'exec git p4son {cmd}')
    return '\n'.join(lines) + '\n'


def _resolve_editor_or_fail(workspace_dir: str) -> str | None:
    """Return the git editor, logging an error if none is configured."""
    editor = resolve_editor(workspace_dir)
    if not editor:
        log.error(
            'No git editor configured. Set one with: git config core.editor <editor>')
    return editor


def run_todo_rebase(todo_content: str, base_branch: str, workspace_dir: str,
                    edit_todo: bool, message: str | None = None,
                    dry_run: bool = False) -> int:
    """Rebase onto base_branch with the generated todo, opening it in the editor if edit_todo.

    A message is saved to MESSAGE_FILE for the todo's exec lines, and left there afterwards.
    """
    if not check_git_workspace_clean(workspace_dir):
        return 1

    if edit_todo:
        log.heading('Checking editor')
        editor = _resolve_editor_or_fail(workspace_dir)
        if not editor:
            return 1
        log.success(editor)

    if dry_run:
        log.info('Generated rebase todo:')
        log.info(todo_content)
        if message is not None:
            log.info(f'Message for {MESSAGE_FILE}:')
            log.info(message)
        return 0

    reviews_dir = _reviews_dir(workspace_dir)
    ensure_config_dir(workspace_dir)
    os.makedirs(reviews_dir, exist_ok=True)
    todo_file = _todo_path(workspace_dir)
    with open(todo_file, 'w') as f:
        f.write(todo_content)

    log.success(f'Saved as {todo_file}')

    if message is not None:
        message_file = os.path.join(workspace_dir, MESSAGE_FILE)
        with open(message_file, 'w', encoding='utf-8') as f:
            f.write(message)
        log.success(f'Saved the message as {message_file}')

    try:
        log.heading('Running interactive rebase')
        env = os.environ.copy()
        env['GIT_SEQUENCE_EDITOR'] = 'git-p4son _sequence-editor'
        if not edit_todo:
            env['GIT_SEQUENCE_EDITOR'] += ' --no-edit'
        result = subprocess.run(
            ['git', 'rebase', '-i', base_branch],
            cwd=workspace_dir,
            env=env,
        )
        if result.returncode != 0:
            log.error('Rebase did not complete successfully.')
            log.error(
                'You can fix any issues and run: git rebase --continue')
            return result.returncode

        log.success('Done')
        return 0
    finally:
        if os.path.exists(todo_file):
            os.remove(todo_file)


def sequence_editor_command(args: argparse.Namespace) -> int:
    """Replace git's rebase todo with ours, then open the user's editor unless --no-edit."""
    workspace_dir = args.workspace_dir
    todo_file = _todo_path(workspace_dir)

    if not os.path.exists(todo_file):
        log.error(f'No review todo file found at {todo_file}')
        return 1

    with open(args.filename, 'r') as f:
        original_lines = f.readlines()
    comment_lines = [line for line in original_lines if line.startswith('#')]

    with open(todo_file, 'r') as f:
        todo_content = f.read()

    # Overwrite the rebase todo file with our version plus git's comments
    with open(args.filename, 'w') as f:
        f.write(todo_content)
        if comment_lines:
            f.write('\n')
            f.writelines(comment_lines)

    if args.no_edit:
        return 0

    editor = _resolve_editor_or_fail(workspace_dir)
    if not editor:
        return 1

    # The editor command may carry arguments, e.g. "code --wait"
    editor_cmd = shlex.split(editor) + [args.filename]
    editor_result = subprocess.run(editor_cmd, cwd=workspace_dir)
    return editor_result.returncode
