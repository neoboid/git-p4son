"""List-changes command implementation for git-p4son."""

import argparse
from .description import number_commits
from .git import get_commit_subjects_since
from .log import log


def get_enumerated_commit_lines_since(base_branch: str, workspace_dir: str) -> list[str]:
    """Get enumerated commit lines from git log since base branch."""
    return number_commits(get_commit_subjects_since(base_branch, workspace_dir))


def get_enumerated_change_description_since(base_branch: str, workspace_dir: str) -> str | None:
    """Get enumerated changelist description, or None if no commits found."""
    lines = get_enumerated_commit_lines_since(base_branch, workspace_dir)

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
