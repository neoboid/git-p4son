"""
Init command implementation for git-p4son.

Sets up a new git repository inside a Perforce workspace with an initial
commit containing .gitignore.
"""

import argparse
import os
import shutil

from .common import CommandError, get_p4_client_name, run, run_with_output
from .log import log
from .review import _resolve_editor


def _check_clobber(cwd: str) -> bool:
    """Check if clobber is enabled on the workspace. Returns True if enabled."""
    try:
        res = run(['p4', 'client', '-o'], cwd=cwd)
        for line in res.stdout:
            stripped = line.strip()
            if stripped.startswith('Options:'):
                options = stripped.split()
                if 'clobber' in options:
                    return True
                return False
        return False
    except (CommandError, OSError):
        return False


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

    log.heading('Checking clobber flag')
    if _check_clobber(cwd):
        log.success('clobber is enabled')
    else:
        log.error(
            f'clobber is not enabled on workspace "{workspace_name}".\n'
            '  Git removes read-only flags when switching branches, so p4 sync\n'
            '  will fail to overwrite those files unless clobber is enabled.\n'
            f'  Edit "{workspace_name}" in P4V to set the clobber flag.')
        return 1

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
