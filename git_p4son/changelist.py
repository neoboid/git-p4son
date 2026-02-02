"""
Changelist command implementation for git-p4son.
"""

import argparse
import os
import re
import sys
import subprocess
from .common import ensure_workspace
from .changelist_store import resolve_changelist, save_changelist_alias
from .list_changes import get_enumerated_change_description_since


def create_changelist(message: str, base_branch: str, workspace_dir: str, dry_run: bool = False) -> tuple[int, str | None]:
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


def get_changelist_spec(changelist_nr: str, workspace_dir: str) -> tuple[int, str | None]:
    """
    Fetch the changelist spec from Perforce.

    Args:
        changelist_nr: The changelist number
        workspace_dir: The workspace directory

    Returns:
        Tuple of (returncode, spec_text or None)
    """
    try:
        result = subprocess.run(
            ['p4', 'change', '-o', changelist_nr],
            cwd=workspace_dir,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            print(f'Failed to get changelist {changelist_nr}', file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return (result.returncode, None)
        return (0, result.stdout)
    except Exception as e:
        print(
            f'Failed to get changelist {changelist_nr}: {e}', file=sys.stderr)
        return (1, None)


def extract_description(spec_text: str) -> str:
    """
    Extract the Description field from a p4 changelist spec.

    The spec has tab-indented continuation lines under the Description: header.

    Returns:
        The description string with tabs stripped.
    """
    lines = spec_text.splitlines()
    description_lines = []
    in_description = False
    for line in lines:
        if line.startswith('Description:'):
            in_description = True
            continue
        if in_description:
            if line.startswith('\t'):
                description_lines.append(line[1:])  # strip leading tab
            else:
                break
    return '\n'.join(description_lines)


def replace_description_in_spec(spec_text: str, new_description: str) -> str:
    """
    Replace the Description field in a p4 changelist spec.

    Args:
        spec_text: The full spec text from p4 change -o
        new_description: The new description string

    Returns:
        The spec text with the description replaced.
    """
    lines = spec_text.splitlines()
    result_lines = []
    in_description = False
    description_replaced = False
    for line in lines:
        if line.startswith('Description:'):
            in_description = True
            result_lines.append(line)
            # Add new description lines, tab-indented
            for desc_line in new_description.splitlines():
                result_lines.append('\t' + desc_line)
            description_replaced = True
            continue
        if in_description:
            if line.startswith('\t'):
                continue  # skip old description lines
            else:
                in_description = False
                result_lines.append(line)
        else:
            result_lines.append(line)
    return '\n'.join(result_lines) + '\n'


def split_description_message_and_commits(description: str) -> tuple[str, str, str]:
    """
    Split a changelist description into the user message, the
    enumerated commit list, and any trailing text.

    The commit list starts at the first line matching "1. " and
    continues as long as lines match "<number>. ". Any text after
    the numbered list is returned as trailing text.

    Args:
        description: The full description string

    Returns:
        Tuple of (user_message, commits_text, trailing_text).
        commits_text and trailing_text may be empty strings.
    """
    lines = description.splitlines()
    # Find start of numbered list
    start = None
    for i, line in enumerate(lines):
        if line.startswith('1. '):
            start = i
            break
    if start is None:
        return (description, '', '')

    # Find end of numbered list (consecutive "<number>. " lines)
    end = start
    expected_nr = 1
    for j in range(start, len(lines)):
        if lines[j].startswith(f'{expected_nr}. '):
            expected_nr += 1
            end = j + 1
        else:
            break

    message = '\n'.join(lines[:start])
    commits = '\n'.join(lines[start:end])
    trailing = '\n'.join(lines[end:])
    return (message, commits, trailing)


def update_changelist(changelist_nr: str, base_branch: str, workspace_dir: str, dry_run: bool = False) -> int:
    """
    Update an existing Perforce changelist by replacing the enumerated
    commit list in the description.

    Args:
        changelist_nr: The changelist number to update
        base_branch: The base branch to compare against for commit list
        workspace_dir: The workspace directory
        dry_run: If True, don't actually update the changelist

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    # Fetch existing spec
    returncode, spec_text = get_changelist_spec(changelist_nr, workspace_dir)
    if returncode != 0:
        return returncode

    # Extract and split description
    old_description = extract_description(spec_text)
    user_message, _, trailing = split_description_message_and_commits(
        old_description)

    # Generate new commit list
    returncode, commits_description = get_enumerated_change_description_since(
        base_branch, workspace_dir)
    if returncode != 0:
        return returncode

    # Rebuild description: message + commits + any trailing text
    new_description = user_message
    if commits_description:
        new_description = user_message + "\n" + commits_description
    if trailing:
        new_description = new_description + "\n" + trailing

    if dry_run:
        print(f"Would update changelist {changelist_nr} with description:")
        print(new_description)
        return 0

    # Replace description in spec and submit
    new_spec = replace_description_in_spec(spec_text, new_description)

    try:
        result = subprocess.run(
            ['p4', 'change', '-i'],
            cwd=workspace_dir,
            input=new_spec,
            capture_output=True,
            text=True
        )
        if result.returncode != 0:
            print(
                f'Failed to update changelist {changelist_nr}', file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return result.returncode
        return 0
    except Exception as e:
        print(
            f'Failed to update changelist {changelist_nr}: {e}', file=sys.stderr)
        return 1


def changelist_new_command(args: argparse.Namespace) -> int:
    """
    Execute the 'changelist new' command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = ensure_workspace()

    # Check alias availability before creating the changelist
    if args.alias and not args.dry_run:
        alias_path = os.path.join(
            workspace_dir, '.git-p4son', 'changelists', args.alias)
        if os.path.exists(alias_path) and not args.force:
            print(f'Alias "{args.alias}" already exists (use -f/--force to overwrite)',
                  file=sys.stderr)
            return 1

    returncode, changelist_number = create_changelist(
        args.message, args.base_branch, workspace_dir, dry_run=args.dry_run)
    if returncode != 0:
        return returncode

    if not args.dry_run:
        print(f"Created changelist {changelist_number}")
        if args.alias:
            if not save_changelist_alias(args.alias, changelist_number,
                                         workspace_dir, force=args.force):
                return 1
            print(f'Saved alias "{args.alias}" -> {changelist_number}')

    return 0


def changelist_update_command(args: argparse.Namespace) -> int:
    """
    Execute the 'changelist update' command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = ensure_workspace()

    changelist = resolve_changelist(args.changelist, workspace_dir)
    if changelist is None:
        return 1

    returncode = update_changelist(
        changelist, args.base_branch, workspace_dir, dry_run=args.dry_run)
    if returncode != 0:
        return returncode

    if not args.dry_run:
        print(f"Updated changelist {changelist}")

    return 0


def changelist_set_command(args: argparse.Namespace) -> int:
    """
    Execute the 'changelist set' command.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    workspace_dir = ensure_workspace()

    if not args.changelist.isdigit():
        print(f'Invalid changelist number: {args.changelist}', file=sys.stderr)
        return 1

    if not save_changelist_alias(args.alias, args.changelist,
                                 workspace_dir, force=args.force):
        return 1

    print(f'Saved alias "{args.alias}" -> {args.changelist}')
    return 0


def changelist_command(args: argparse.Namespace) -> int:
    """
    Dispatch changelist subcommands.

    Args:
        args: Parsed command line arguments

    Returns:
        Exit code (0 for success, non-zero for failure)
    """
    if args.changelist_action == 'new':
        return changelist_new_command(args)
    elif args.changelist_action == 'update':
        return changelist_update_command(args)
    elif args.changelist_action == 'set':
        return changelist_set_command(args)
    else:
        print('No changelist action specified. Use "git-p4son changelist -h" for help.',
              file=sys.stderr)
        return 1
