"""
Changelist command implementation for pergit.
"""

import re
import sys
import subprocess
from .common import ensure_workspace
from .list_changes import get_enumerated_change_description_since


def create_changelist(message, base_branch, workspace_dir, dry_run=False):
    """
    Create a new Perforce changelist with the given message and
    enumerated git commits as description.

    Args:
        message: User-provided changelist description
        base_branch: The base branch to compare against for commit list
        workspace_dir: The workspace directory
        dry_run: If True, don't actually create the changelist

    Returns:
        Tuple of (returncode, changelist_number or None)
    """
    # Build description: user message + enumerated commits
    returncode, commits_description = get_enumerated_change_description_since(
        base_branch, workspace_dir)
    if returncode != 0:
        return (returncode, None)

    description = message
    if commits_description:
        description = message + "\n" + commits_description

    if dry_run:
        print(f"Would create new changelist with description:")
        print(description)
        return (0, None)

    # Prepare the changelist spec content
    tabbed_description = "\n\t".join(description.splitlines())
    spec_content = f"Change: new\n\nDescription:\n\t{tabbed_description}\n"

    # Create the changelist using p4 change -i
    try:
        result = subprocess.run(
            ['p4', 'change', '-i'],
            cwd=workspace_dir,
            input=spec_content,
            capture_output=True,
            text=True
        )

        if result.returncode != 0:
            print('Failed to create new changelist', file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return (result.returncode, None)

        # Extract changelist number from output
        # Format: "Change 12345 created."
        changelist_number = None
        for line in result.stdout.splitlines():
            if 'Change' in line and 'created' in line:
                match = re.search(r'Change (\d+) created', line)
                if match:
                    changelist_number = match.group(1)
                    break

        if changelist_number is None:
            print(
                'Failed to extract changelist number from p4 change output',
                file=sys.stderr)
            print('Output:', result.stdout, file=sys.stderr)
            return (1, None)

        return (0, changelist_number)

    except Exception as e:
        print(f'Failed to create new changelist: {e}', file=sys.stderr)
        return (1, None)


def changelist_new_command(args):
    """
    Execute the 'changelist new' command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = ensure_workspace()

    returncode, changelist_number = create_changelist(
        args.message, args.base_branch, workspace_dir, dry_run=args.dry_run)
    if returncode != 0:
        return returncode

    if not args.dry_run:
        print(f"Created changelist {changelist_number}")

    return 0


def changelist_command(args):
    """
    Dispatch changelist subcommands.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    if args.changelist_action == 'new':
        return changelist_new_command(args)
    else:
        print('No changelist action specified. Use "pergit changelist -h" for help.',
              file=sys.stderr)
        return 1
