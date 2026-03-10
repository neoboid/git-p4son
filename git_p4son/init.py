"""
Init command implementation for git-p4son.

Sets up a new git repository inside a Perforce workspace with an initial
commit containing .gitignore.
"""

import argparse
import os
import shutil

from .common import CommandError, get_p4_client_name, run, run_with_output
from .config import get_depot_root, save_config
from .log import log
from .review import _resolve_editor


def _get_p4_client_spec(cwd: str) -> list[str]:
    """Get the raw p4 client spec lines."""
    res = run(['p4', 'client', '-o'], cwd=cwd)
    return res.stdout


def _check_clobber(spec_lines: list[str]) -> bool:
    """Check if clobber is enabled in a client spec."""
    for line in spec_lines:
        stripped = line.strip()
        if stripped.startswith('Options:'):
            return 'clobber' in stripped.split()
    return False


def _get_p4_workspace_root(spec_lines: list[str]) -> str | None:
    """Extract the Root path from a client spec."""
    for line in spec_lines:
        stripped = line.strip()
        if stripped.startswith('Root:'):
            parts = stripped.split('\t', 1)
            if len(parts) == 2:
                return parts[1].strip()
    return None


def _validate_depot_root(depot_root: str, cwd: str) -> bool:
    """Validate a depot root by querying p4 for changelists."""
    try:
        run(['p4', 'changes', '-m1', '-s', 'submitted',
             f'{depot_root}/...'], cwd=cwd)
        return True
    except CommandError:
        return False


def _compute_cwd_depot_root(client_name: str, cwd: str,
                            p4_workspace_root: str) -> str | None:
    """Compute depot root for cwd relative to workspace root.

    Returns None if cwd is the workspace root (identical to entire workspace).
    """
    rel = os.path.relpath(cwd, p4_workspace_root)
    if rel == '.':
        return None
    rel_posix = rel.replace(os.sep, '/')
    return f'//{client_name}/{rel_posix}'


def _select_depot_root(client_name: str, cwd: str,
                       p4_workspace_root: str) -> str | None:
    """Interactive prompt for depot root selection. Returns root or None to abort."""
    entire_root = f'//{client_name}'
    cwd_root = _compute_cwd_depot_root(client_name, cwd, p4_workspace_root)

    while True:
        print()
        print('Select the depot path that git-p4son should sync:')
        print(f'  1. Entire workspace: {entire_root}/...')
        if cwd_root:
            print(f'  2. Current directory: {cwd_root}/...')
            abort_num = '3'
        else:
            abort_num = '2'
        print(f'  {abort_num}. Abort')
        print()

        choice = input('Choice [1]: ').strip()
        if choice == '' or choice == '1':
            depot_root = entire_root
        elif choice == '2' and cwd_root:
            depot_root = cwd_root
        elif choice == abort_num:
            return None
        else:
            print(f'Invalid choice: {choice}')
            continue

        if _validate_depot_root(depot_root, cwd):
            return depot_root

        log.error(f'Depot root {depot_root}/... is not valid')


def _configure_depot_root(client_name: str, cwd: str,
                          p4_workspace_root: str) -> str | None:
    """Configure depot root: validate existing or prompt for new.

    Returns the depot root, or None if aborted.
    """
    existing_root = get_depot_root(cwd)
    if existing_root:
        if _validate_depot_root(existing_root, cwd):
            log.success(f'{existing_root}/...')
            return existing_root
        log.warning(
            f'Saved depot root {existing_root}/... is no longer valid')

    return _select_depot_root(client_name, cwd, p4_workspace_root)


def _setup_gitignore(cwd: str) -> str:
    """Set up .gitignore file. Returns a description of what was done."""
    gitignore_path = os.path.join(cwd, '.gitignore')
    p4ignore_path = os.path.join(cwd, '.p4ignore')

    if os.path.exists(gitignore_path):
        return '.gitignore already exist'

    if os.path.exists(p4ignore_path):
        shutil.copy2(p4ignore_path, gitignore_path)
        return 'copied .p4ignore to new .gitignore'

    with open(gitignore_path, 'w') as f:
        pass
    return 'created empty .gitignore'


def init_command(args: argparse.Namespace) -> int:
    """Execute the init command."""
    cwd = os.getcwd()

    log.heading('Checking Perforce workspace')
    workspace_name = get_p4_client_name(cwd)
    if workspace_name:
        log.success(workspace_name)
    else:
        log.error('Not inside a Perforce workspace. '
                  'Is Perforce installed and configured?')
        return 1

    spec_lines = _get_p4_client_spec(cwd)

    log.heading('Checking clobber flag')
    if _check_clobber(spec_lines):
        log.success('clobber is enabled')
    else:
        log.error(
            f'clobber is not enabled on workspace "{workspace_name}".\n'
            '  Git removes read-only flags when switching branches, so p4 sync\n'
            '  will fail to overwrite those files unless clobber is enabled.\n'
            f'  Edit "{workspace_name}" in P4V to set the clobber flag.')
        return 1

    log.heading('Find perforce workspace root')
    p4_workspace_root = _get_p4_workspace_root(spec_lines)
    if not p4_workspace_root:
        log.error('Failed to determine workspace root from p4 client spec')
        return 1
    log.success(p4_workspace_root)

    log.heading('Configuring depot root')
    depot_root = _configure_depot_root(workspace_name, cwd, p4_workspace_root)
    if not depot_root:
        log.error('Aborted')
        return 1
    save_config(cwd, {'depot': {'root': depot_root}})

    log.heading('Checking .gitignore')
    result = _setup_gitignore(cwd)
    log.success(result)

    log.heading('Checking git repo')
    git_dir = os.path.join(cwd, '.git')
    existing_repo = os.path.exists(git_dir)
    if existing_repo:
        log.success('.git/ already exists')
    else:
        run_with_output(['git', 'init'], cwd=cwd)
        log.success('created new git repository')

    if not existing_repo:
        log.heading('Creating initial commit')
        run_with_output(['git', 'add', '.gitignore'], cwd=cwd)
        run_with_output(
            ['git', 'commit', '-m', 'Initialize git-p4son repository'],
            cwd=cwd)

        log.heading('Next steps')
        log.info('* Review and edit .gitignore')
        log.info('* git add .')
        log.info('* git commit -m "Initial commit"')
        log.info('* git p4son sync')

    # Nudge user to set an editor if none is configured
    log.heading('Validating git editor configuration')
    editor = _resolve_editor(cwd)
    if editor:
        log.success(editor)
    else:
        log.warning(
            'No editor configured. Set one with: git config core.editor <editor>')

    return 0
