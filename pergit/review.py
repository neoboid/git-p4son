"""
Review command implementation for pergit.
"""

import argparse
import os
import re
import subprocess
import sys
from .common import ensure_workspace, run
from .changelist import create_changelist, update_changelist
from .changelist_store import resolve_changelist, save_changelist_alias
from .edit import get_local_git_changes, include_changes_in_changelist


def p4_shelve_changelist(changelist: str, workspace_dir: str, dry_run: bool = False) -> int:
    """
    Shelve a changelist to make it available for review.

    Args:
        changelist: The changelist number to shelve
        workspace_dir: The workspace directory
        dry_run: If True, don't actually shelve

    Returns:
        Exit code (0 for success, non-zero for failure)
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


def add_review_keyword_to_changelist(changelist: str, workspace_dir: str, dry_run: bool = False) -> tuple[int, bool]:
    """
    Add the #review keyword to a changelist description.

    Args:
        changelist: The changelist number to update
        workspace_dir: The workspace directory
        dry_run: If True, don't actually update

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Get current changelist description
    res = run(['p4', 'change', '-o', changelist], cwd=workspace_dir)
    if res.returncode != 0:
        print('Failed to get changelist description', file=sys.stderr)
        return res.returncode

    # Parse the changelist spec to find description and track its end
    lines = res.stdout
    description_start_idx = None
    description_end_idx = None

    for i, line in enumerate(lines):
        if line.strip() == 'Description:':
            description_start_idx = i
        elif description_start_idx is not None and description_end_idx is None:
            # Description ends when we hit the next field header (non-indented line with a colon)
            if re.match(r'^[A-Za-z].*:$', line.strip()):
                description_end_idx = i
                break

    # If we didn't find another field header, description goes to end
    if description_start_idx is not None and description_end_idx is None:
        description_end_idx = len(lines)

    # Check if #review is already in the description
    if description_start_idx is not None:
        description_text = '\n'.join(
            lines[description_start_idx:description_end_idx])
        if '#review' in description_text:
            print(f'Changelist {changelist} already has #review keyword')
            return 0

    # Add #review as the last line of description
    updated_lines = lines.copy()
    if description_start_idx is not None:
        # Insert #review before the empty line that ends the description
        updated_lines.insert(description_end_idx, '\t#review')

    # Update the changelist
    if dry_run:
        print(f"Would add #review keyword to changelist {changelist}")
        return 0

    try:
        result = subprocess.run(
            ['p4', 'change', '-i'],
            cwd=workspace_dir,
            input='\n'.join(updated_lines),
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print('Failed to update changelist description', file=sys.stderr)
            print(result.stderr, file=sys.stderr)

        return result.returncode

    except Exception as e:
        print(f'Failed to update changelist description: {e}', file=sys.stderr)
        return 1


def review_new_command(args: argparse.Namespace) -> int:
    """
    Execute the review new command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = ensure_workspace()

    # Check alias availability before creating the changelist
    if args.alias and not args.dry_run:
        alias_path = os.path.join(
            workspace_dir, '.pergit', 'changelists', args.alias)
        if os.path.exists(alias_path) and not args.force:
            print(f'Alias "{args.alias}" already exists (use -f/--force to overwrite)',
                  file=sys.stderr)
            return 1

    # Create new changelist
    returncode, changelist = create_changelist(
        args.message, args.base_branch, workspace_dir, dry_run=args.dry_run)
    if returncode != 0:
        print('Failed to create new changelist', file=sys.stderr)
        return returncode

    if not args.dry_run:
        print(f"Created new changelist: {changelist}")
        if args.alias:
            if not save_changelist_alias(args.alias, changelist,
                                         workspace_dir, force=args.force):
                return 1
            print(f'Saved alias "{args.alias}" -> {changelist}')

    # Open changed files for edit in the new changelist
    returncode = open_changes_for_edit(
        args.base_branch, changelist, workspace_dir, args.dry_run)
    if returncode != 0:
        return returncode

    # Add #review keyword to changelist description
    returncode = add_review_keyword_to_changelist(
        changelist, workspace_dir, dry_run=args.dry_run)
    if returncode != 0:
        return returncode

    # Shelve the changelist to create the review
    returncode = p4_shelve_changelist(
        changelist, workspace_dir, dry_run=args.dry_run)
    if returncode != 0:
        return returncode

    if not args.dry_run:
        print(f"Created Swarm review for changelist {changelist}")
    return 0


def review_update_command(args: argparse.Namespace) -> int:
    """
    Execute the review update command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = ensure_workspace()

    changelist = resolve_changelist(args.changelist, workspace_dir)
    if changelist is None:
        return 1

    if not changelist.isdigit():
        print('Invalid changelist number: %s' %
              changelist, file=sys.stderr)
        return 1

    # Optionally update the changelist description
    if args.description:
        returncode = update_changelist(
            changelist, args.base_branch, workspace_dir, dry_run=args.dry_run)
        if returncode != 0:
            return returncode

    # Open changed files for edit in the changelist
    returncode = open_changes_for_edit(
        args.base_branch, changelist, workspace_dir, args.dry_run)
    if returncode != 0:
        return returncode

    # Re-shelve the changelist to update the review
    returncode = p4_shelve_changelist(
        changelist, workspace_dir, dry_run=args.dry_run)
    if returncode != 0:
        return returncode

    print(f"Updated Swarm review for changelist {changelist}")

    return 0


def review_command(args: argparse.Namespace) -> int:
    """
    Dispatch review subcommands.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    if args.review_action == 'new':
        return review_new_command(args)
    elif args.review_action == 'update':
        return review_update_command(args)
    else:
        print('No review action specified. Use "pergit review -h" for help.',
              file=sys.stderr)
        return 1
