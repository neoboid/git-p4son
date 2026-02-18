"""
Review command implementation for git-p4son.

Automates the interactive rebase workflow by generating a rebase todo file
with exec lines that run git p4son new/update for each commit.
"""

import argparse
import os
import shlex
import subprocess
import sys
from .common import run


def _reviews_dir(workspace_dir: str) -> str:
    """Return the path to the reviews directory."""
    return os.path.join(workspace_dir, '.git-p4son', 'reviews')


def _todo_path(workspace_dir: str) -> str:
    """Return the path to the generated todo file."""
    return os.path.join(_reviews_dir(workspace_dir), 'todo')


def _get_commit_lines(base_branch: str, workspace_dir: str) -> list[str]:
    """Get git log --oneline lines for commits since base branch.

    Returns:
        List of "hash subject" lines.
    """
    res = run(['git', 'log', '--oneline', '--reverse',
               f'{base_branch}..HEAD'], cwd=workspace_dir)
    return res.stdout


def _generate_todo(commit_lines: list[str], alias: str, message: str,
                   force: bool) -> str:
    """Generate the rebase todo content with exec lines.

    Args:
        commit_lines: Lines from git log --oneline (hash + subject)
        alias: The changelist alias name
        message: The changelist description for the new command
        force: Whether to pass --force to the new command

    Returns:
        The generated todo file content
    """
    lines = []
    last_index = len(commit_lines) - 1
    for i, commit_line in enumerate(commit_lines):
        parts = commit_line.split(' ', 1)
        commit_hash = parts[0]
        subject = parts[1] if len(parts) > 1 else ''

        lines.append(f'pick {commit_hash} {subject}')

        if i == 0:
            # First commit: create new changelist with review
            cmd = f'new {shlex.quote(alias)} --review -m {shlex.quote(message)}'
            if force:
                cmd += ' --force'
        else:
            # Subsequent commits: update and shelve
            cmd = f'update {shlex.quote(alias)} --shelve'

        # Sleep after all exec lines except the last
        if i < last_index:
            cmd += ' --sleep 5'
        lines.append(f'exec git p4son {cmd}')

    return '\n'.join(lines) + '\n'


def review_command(args: argparse.Namespace) -> int:
    """
    Execute the 'review' command.

    Generates a rebase todo with exec lines and runs git rebase -i
    with GIT_SEQUENCE_EDITOR set to the _sequence-editor subcommand.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = args.workspace_dir

    # Check alias availability before starting
    if not args.dry_run:
        alias_path = os.path.join(
            workspace_dir, '.git-p4son', 'changelists', args.alias)
        if os.path.exists(alias_path) and not args.force:
            print(f'Alias "{args.alias}" already exists (use -f/--force to overwrite)',
                  file=sys.stderr)
            return 1

    # Get commits since base branch
    commit_lines = _get_commit_lines(args.base_branch, workspace_dir)

    if not commit_lines:
        print('No commits found since {}'.format(args.base_branch),
              file=sys.stderr)
        return 1

    # Generate the rebase todo
    todo_content = _generate_todo(
        commit_lines, args.alias, args.message, args.force)

    if args.dry_run:
        print('Generated rebase todo:')
        print(todo_content)
        return 0

    # Write todo to .git-p4son/reviews/todo
    reviews_dir = _reviews_dir(workspace_dir)
    os.makedirs(reviews_dir, exist_ok=True)
    todo_file = _todo_path(workspace_dir)
    with open(todo_file, 'w') as f:
        f.write(todo_content)

    try:
        # Run git rebase -i with our sequence editor
        env = os.environ.copy()
        env['GIT_SEQUENCE_EDITOR'] = 'git p4son _sequence-editor'
        result = subprocess.run(
            ['git', 'rebase', '-i', args.base_branch],
            cwd=workspace_dir,
            env=env,
        )
        if result.returncode != 0:
            print('\nRebase did not complete successfully.',
                  file=sys.stderr)
            print('You can fix any issues and run: git rebase --continue',
                  file=sys.stderr)
            return result.returncode

        return 0
    finally:
        # Clean up the todo file
        if os.path.exists(todo_file):
            os.remove(todo_file)


def sequence_editor_command(args: argparse.Namespace) -> int:
    """
    Execute the '_sequence-editor' hidden subcommand.

    Called by git as GIT_SEQUENCE_EDITOR. Overwrites the todo file with
    our generated content, then opens the user's real editor.

    Args:
        args: Parsed command line arguments (args.filename is the todo file)

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = args.workspace_dir
    todo_file = _todo_path(workspace_dir)

    if not os.path.exists(todo_file):
        print('No review todo file found at {}'.format(todo_file),
              file=sys.stderr)
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

    # Resolve the user's editor via git var GIT_EDITOR
    result = subprocess.run(
        ['git', 'var', 'GIT_EDITOR'],
        capture_output=True,
        text=True,
        cwd=workspace_dir,
    )
    if result.returncode != 0:
        print('Failed to resolve editor via git var GIT_EDITOR',
              file=sys.stderr)
        return 1

    editor = result.stdout.strip()

    # Open the editor on the todo file
    # The editor command may contain arguments (e.g. "code --wait"),
    # so we need to split it
    editor_cmd = shlex.split(editor) + [args.filename]
    editor_result = subprocess.run(editor_cmd, cwd=workspace_dir)
    return editor_result.returncode
