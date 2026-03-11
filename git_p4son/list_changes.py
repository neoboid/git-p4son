"""
List-changes command implementation for git-p4son.
"""

import argparse
from .git import get_commit_subjects_since
from .log import log


def get_enumerated_commit_lines_since(base_branch: str, workspace_dir: str, start_number: int = 1) -> list[str]:
    """Get enumerated commit lines from git log since base branch."""
    subjects = get_commit_subjects_since(base_branch, workspace_dir)
    return [f"{i}. {subject}"
            for i, subject in enumerate(subjects, start_number)]


def get_enumerated_change_description_since(base_branch: str, workspace_dir: str, start_number: int = 1) -> str | None:
    """Get enumerated changelist description, or None if no commits found."""
    lines = get_enumerated_commit_lines_since(
        base_branch, workspace_dir, start_number)

    if not lines:
        return None

    return '\n'.join(lines)


def list_changes_command(args: argparse.Namespace) -> int:
    """Execute the list-changes command."""
    description = get_enumerated_change_description_since(
        args.base_branch, args.workspace_dir)

    if description:
        log.info(description)
    else:
        log.success("No changes found")

    return 0
