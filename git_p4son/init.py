"""
Init command implementation for git-p4son.

Sets up a new git repository inside a Perforce workspace with an initial
commit containing .gitignore.
"""

import argparse
import os
import shutil

from .common import CommandError, run, run_with_output
from .config import save_config
from .depot import WORKSPACE_PLACEHOLDER, expand_depot_root, get_depot_root
from .log import log
from .perforce import get_client_spec
from .git import resolve_editor
from .writable import is_writable_mode, set_writable_mode


def _validate_depot_root(depot_root: str, cwd: str) -> bool:
    """Validate a depot root by querying p4 for changelists."""
    try:
        run(['p4', 'changes', '-m1', '-s', 'submitted',
             f'{depot_root}/...'], cwd=cwd)
        return True
    except CommandError:
        return False


def _compute_cwd_depot_root(cwd: str, p4_workspace_root: str) -> str | None:
    """Compute the depot root template for cwd relative to workspace root.

    Uses the $(workspace) placeholder in place of the client name so the stored
    root survives a workspace rename. Returns None if cwd is the workspace root
    (identical to the entire workspace).
    """
    rel = os.path.relpath(cwd, p4_workspace_root)
    if rel == '.':
        return None
    rel_posix = rel.replace(os.sep, '/')
    return f'//{WORKSPACE_PLACEHOLDER}/{rel_posix}'


def _select_depot_root(client_name: str, cwd: str,
                       p4_workspace_root: str) -> str | None:
    """Interactive prompt for depot root selection.

    Returns a depot root template (with the $(workspace) placeholder) to store,
    or None to abort. Menu entries and validation use the resolved path.
    """
    entire_root = f'//{WORKSPACE_PLACEHOLDER}'
    cwd_root = _compute_cwd_depot_root(cwd, p4_workspace_root)

    while True:
        print()
        print('Select the depot path that git-p4son should sync:')
        print(f'  1. Entire workspace: '
              f'{expand_depot_root(entire_root, client_name)}/...')
        if cwd_root:
            print(f'  2. Current directory: '
                  f'{expand_depot_root(cwd_root, client_name)}/...')
            abort_num = '3'
        else:
            abort_num = '2'
        print(f'  {abort_num}. Abort')
        print()

        try:
            choice = input('Choice [1]: ').strip()
        except EOFError:
            print()
            return None
        if choice == '' or choice == '1':
            depot_root = entire_root
        elif choice == '2' and cwd_root:
            depot_root = cwd_root
        elif choice == abort_num:
            return None
        else:
            print(f'Invalid choice: {choice}')
            continue

        resolved = expand_depot_root(depot_root, client_name)
        if _validate_depot_root(resolved, cwd):
            return depot_root

        log.error(f'Depot root {resolved}/... is not valid')


def _configure_depot_root(client_name: str, cwd: str,
                          p4_workspace_root: str) -> bool:
    """Configure depot root: validate existing or prompt for new."""
    log.heading('Finding git-p4son depot root')
    depot_root = get_depot_root(cwd)
    if depot_root:
        log.success(f'{expand_depot_root(depot_root, client_name)}/...')
    else:
        log.warning('no root configured')

    if depot_root:
        log.heading('Validating depot root')
        resolved = expand_depot_root(depot_root, client_name)
        if _validate_depot_root(resolved, cwd):
            log.success('all good')
            return True
        else:
            log.error(f'{resolved}/... is not valid')
            depot_root = None

    if not depot_root:
        log.heading('Configuring depot root')
        depot_root = _select_depot_root(client_name, cwd, p4_workspace_root)
        if not depot_root:
            log.error('aborting')
            return False

    log.success(f'{expand_depot_root(depot_root, client_name)}/...')
    save_config(cwd, {'depot': {'root': depot_root}})
    return True


def _ask_writable_mode(current: bool) -> bool | None:
    """Ask whether to keep git-tracked files writable.

    An empty answer keeps the current value. Returns None on EOF."""
    choices = '[Y/n]' if current else '[y/N]'
    while True:
        try:
            answer = input(
                f'Keep git-tracked files writable? {choices}: ').strip().lower()
        except EOFError:
            print()
            return None
        if answer == '':
            return current
        if answer in ('y', 'yes'):
            return True
        if answer in ('n', 'no'):
            return False
        print('Please enter y or n')


def _configure_writable_mode(cwd: str) -> tuple[bool, bool]:
    """Ask for the writable mode setting and save it.

    Only the setting is saved: file permissions are left alone, since on a
    fresh repo the files are not tracked yet. Returns (enabled, changed)."""
    log.heading('Configuring writable mode')
    current = is_writable_mode(cwd)
    print()
    print('Writable mode keeps git-tracked files writable, so they can be')
    print('edited without p4 edit. Git-ignored files stay read-only.')
    enabled = _ask_writable_mode(current)
    if enabled is None:
        log.success(f'{"on" if current else "off"} (unchanged)')
        return current, False
    set_writable_mode(cwd, enabled)
    log.success('on' if enabled else 'off')
    return enabled, enabled != current


def _has_commits(cwd: str) -> bool:
    """Return whether the git repo has any commit (HEAD resolves)."""
    result = run(['git', 'rev-parse', '--verify', '--quiet', 'HEAD'],
                 cwd=cwd, fail_on_returncode=False)
    return result.returncode == 0


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
    spec = get_client_spec(cwd)
    if not spec:
        log.error('Not inside a Perforce workspace. '
                  'Is Perforce installed and configured?')
        return 1
    log.success(spec.name)

    if not _configure_depot_root(spec.name, cwd, spec.root):
        return 1

    writable, writable_changed = _configure_writable_mode(cwd)

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

    # An existing repo without commits also needs the initial commit: a
    # previous init may have failed at the commit step (e.g. user.email
    # not configured), and sync cannot work on an unborn HEAD.
    if not _has_commits(cwd):
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
        if writable:
            log.info('* git p4son writable apply')
    elif writable_changed:
        log.heading('Next steps')
        log.info('* git p4son writable apply, to update the tracked files')

    # Nudge user to set an editor if none is configured
    log.heading('Validating git editor configuration')
    editor = resolve_editor(cwd)
    if editor:
        log.success(editor)
    else:
        log.warning(
            'No editor configured. Set one with: git config core.editor <editor>')

    return 0
