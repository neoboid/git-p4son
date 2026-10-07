"""
Rebase todo support for git-p4son.

Runs an interactive rebase with a generated todo, whose exec lines run
git p4son after each picked commit. The hidden _sequence-editor command puts
the generated todo in place of git's own.
"""

import argparse
import os
import shlex
import subprocess
from . import CONFIG_DIR
from .config import ensure_config_dir
from .git import resolve_editor
from .log import log


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


def run_todo_rebase(todo_content: str, base_branch: str, workspace_dir: str,
                    edit_todo: bool) -> int:
    """Rebase onto base_branch with the generated todo.

    With edit_todo the todo is opened in the git editor before the rebase
    runs, as with a normal git rebase -i."""
    reviews_dir = _reviews_dir(workspace_dir)
    ensure_config_dir(workspace_dir)
    os.makedirs(reviews_dir, exist_ok=True)
    todo_file = _todo_path(workspace_dir)
    with open(todo_file, 'w') as f:
        f.write(todo_content)

    log.success(f'Saved as {todo_file}')

    try:
        # Run git rebase -i with our sequence editor
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
        # Clean up the todo file
        if os.path.exists(todo_file):
            os.remove(todo_file)


def sequence_editor_command(args: argparse.Namespace) -> int:
    """Replace git's rebase todo with ours, then open the user's editor
    unless --no-edit is given."""
    workspace_dir = args.workspace_dir
    todo_file = _todo_path(workspace_dir)

    if not os.path.exists(todo_file):
        log.error(f'No review todo file found at {todo_file}')
        return 1

    # Read the original git todo file to preserve comment lines
    with open(args.filename, 'r') as f:
        original_lines = f.readlines()
    comment_lines = [line for line in original_lines if line.startswith('#')]

    # Read our generated todo
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

    editor = resolve_editor(workspace_dir)
    if not editor:
        log.error(
            'No git editor configured. Set one with: git config core.editor <editor>')
        return 1

    # Open the editor on the todo file
    # The editor command may contain arguments (e.g. "code --wait"),
    # so we need to split it
    editor_cmd = shlex.split(editor) + [args.filename]
    editor_result = subprocess.run(editor_cmd, cwd=workspace_dir)
    return editor_result.returncode
