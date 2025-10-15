"""
Review command implementation for pergit.
"""

import argparse
import re
import subprocess
import sys
from .common import ensure_workspace, run
from .edit import get_local_git_changes, include_changes_in_changelist


def p4_shelve_changelist(changelist: str, workspace_dir: str, dry_run: bool = False) -> int:
    """
    Shelve a changelist to make it available for review.

    Args:
        changelist: The changelist number to shelve
        workspace_dir: The workspace directory
        dry_run: If True, don't actually shelve

    Returns:
        Tuple of (returncode, success)
    """
    res = run(['p4', 'shelve', '-f', '-Af', '-c', changelist],
              cwd=workspace_dir, dry_run=dry_run)

    if res.returncode != 0:
        print('Failed to shelve changelist', file=sys.stderr)

    return res.returncode


def open_changes_for_edit(base_branch: str, changelist: str, workspace_dir: str, dry_run: bool = False) -> int:
    """
    Get local git changes and open them for edit in a Perforce changelist.

    Args:
        base_branch: The base branch to compare against
        changelist: The changelist number to add files to
        workspace_dir: The workspace directory
        dry_run: If True, don't actually execute commands

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    returncode, changes = get_local_git_changes(base_branch, workspace_dir)
    if returncode != 0:
        print('Failed to get a list of changed files', file=sys.stderr)
        return returncode

    return include_changes_in_changelist(changes, changelist, workspace_dir, dry_run)


def review_update_command(args: argparse.Namespace) -> int:
    """
    Execute the review update command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = ensure_workspace()

    if not args.changelist.isdigit():
        print('Invalid changelist number: %s' %
              args.changelist, file=sys.stderr)
        return 1

    # Open changed files for edit in the changelist
    returncode = open_changes_for_edit(
        args.base_branch, args.changelist, workspace_dir, args.dry_run)
    if returncode != 0:
        return returncode

    # Re-shelve the changelist to update the review
    returncode = p4_shelve_changelist(
        args.changelist, workspace_dir, dry_run=args.dry_run)
    if returncode != 0:
        return returncode

    print(f"Updated Swarm review for changelist {args.changelist}")

    return 0


def review_command(args: argparse.Namespace) -> int:
    """
    Dispatch review subcommands.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    if args.review_action == 'update':
        return review_update_command(args)
    else:
        print('No review action specified. Use "pergit review -h" for help.',
              file=sys.stderr)
        return 1
