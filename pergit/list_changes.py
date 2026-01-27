"""
List-changes command implementation for pergit.
"""

import argparse
import sys
from .common import ensure_workspace, run


def get_commit_subjects_since(base_branch: str, workspace_dir: str) -> tuple[int, list[str] | None]:
    """
    Get list of commit subjects from git log since base branch.

    Args:
        base_branch: The base branch to compare against
        workspace_dir: The workspace directory

    Returns:
        Tuple of (returncode, list_of_subjects or None)
    """
    # Run git log to get commit subjects since base branch
    # Using --reverse to get oldest commits first
    res = run(['git', 'log', '--oneline', '--reverse', '{}..HEAD'.format(base_branch)],
              cwd=workspace_dir)

    if res.returncode != 0:
        return (res.returncode, None)

    # Extract just the subjects (everything after the hash and space)
    subjects = []
    for line in res.stdout:
        if ' ' in line:
            subject = line.split(' ', 1)[1]
            subjects.append(subject)
        else:
            # Fallback if format is unexpected
            subjects.append(line)

    return (0, subjects)


def get_enumerated_change_description_since(base_branch: str, workspace_dir: str) -> tuple[int, str | None]:
    """
    Get changelist description from git log since base branch.

    Args:
        base_branch: The base branch to compare against
        workspace_dir: The workspace directory

    Returns:
        Tuple of (returncode, description_string or None)
    """
    returncode, subjects = get_commit_subjects_since(
        base_branch, workspace_dir)
    if returncode != 0:
        return (returncode, None)

    if not subjects:
        return (0, None)

    description_lines = []
    for i, subject in enumerate(subjects, 1):
        description_lines.append(f"{i}. {subject}")

    return (0, '\n'.join(description_lines))


def list_changes_command(args: argparse.Namespace) -> int:
    """
    Execute the list-changes command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = ensure_workspace()

    returncode, description = get_enumerated_change_description_since(
        args.base_branch, workspace_dir)
    if returncode != 0:
        print('Failed to get commit list', file=sys.stderr)
        return returncode

    if description:
        print(description)
    else:
        print("No changes found")

    return 0
