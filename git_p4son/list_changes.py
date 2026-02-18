"""
List-changes command implementation for git-p4son.
"""

import argparse
from .common import run


def get_commit_subjects_since(base_branch: str, workspace_dir: str) -> list[str]:
    """
    Get list of commit subjects from git log since base branch.

    Args:
        base_branch: The base branch to compare against
        workspace_dir: The workspace directory

    Returns:
        List of commit subjects.
    """
    # Run git log to get commit subjects since base branch
    # Using --reverse to get oldest commits first
    res = run(['git', 'log', '--oneline', '--reverse', '{}..HEAD'.format(base_branch)],
              cwd=workspace_dir)

    # Extract just the subjects (everything after the hash and space)
    subjects = []
    for line in res.stdout:
        if ' ' in line:
            subject = line.split(' ', 1)[1]
            subjects.append(subject)
        else:
            # Fallback if format is unexpected
            subjects.append(line)

    return subjects


def get_enumerated_commit_lines_since(base_branch: str, workspace_dir: str, start_number: int = 1) -> list[str]:
    """
    Get enumerated commit lines from git log since base branch.

    Args:
        base_branch: The base branch to compare against
        workspace_dir: The workspace directory
        start_number: The starting number for enumeration (default 1)

    Returns:
        List of enumerated commit lines.
    """
    subjects = get_commit_subjects_since(base_branch, workspace_dir)

    lines = []
    for i, subject in enumerate(subjects, start_number):
        lines.append(f"{i}. {subject}")

    return lines


def get_enumerated_change_description_since(base_branch: str, workspace_dir: str, start_number: int = 1) -> str | None:
    """
    Get changelist description from git log since base branch.

    Args:
        base_branch: The base branch to compare against
        workspace_dir: The workspace directory
        start_number: The starting number for enumeration (default 1)

    Returns:
        Description string, or None if no commits found.
    """
    lines = get_enumerated_commit_lines_since(
        base_branch, workspace_dir, start_number)

    if not lines:
        return None

    return '\n'.join(lines)


def list_changes_command(args: argparse.Namespace) -> int:
    """
    Execute the list-changes command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    description = get_enumerated_change_description_since(
        args.base_branch, args.workspace_dir)

    if description:
        print(description)
    else:
        print("No changes found")

    return 0
