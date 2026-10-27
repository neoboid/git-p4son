"""List-changes command implementation for git-p4son."""

import argparse
from .description import number_commits
from .git import get_commit_subjects_since
from .log import log


def list_changes_command(args: argparse.Namespace) -> int:
    """Execute the list-changes command."""
    log.heading('Listing changes')
    subjects = get_commit_subjects_since(args.base_branch, args.workspace_dir,
                                         args.backend.revision)
    if subjects:
        print('\n'.join(number_commits(subjects)))
    else:
        log.success("No changes found")
    return 0
