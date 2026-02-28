"""
Init command implementation for git-p4son.

Sets up a new git repository inside a Perforce workspace with an initial
commit containing .gitignore.
"""

import argparse
import os
import shutil

from .common import CommandError, run, run_with_output
from .log import log


def _get_p4_workspace_name(cwd: str) -> str | None:
    """Get the Perforce workspace name by running p4 info.

    Returns the client name on success, or None on failure.
    """
    try:
        res = run(['p4', 'info'], cwd=cwd)
        for line in res.stdout:
            if line.startswith('Client name:'):
                client_name = line.split(':', 1)[1].strip()
                if client_name != '*unknown*':
                    log.detail('client', client_name)
                    return client_name
        log.error('Not inside a Perforce workspace')
        return None
    except (CommandError, OSError):
        log.error('Failed to run p4 info. Is Perforce installed and configured?')
        return None


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
        return 'using existing .gitignore'

    if os.path.exists(p4ignore_path):
        shutil.copy2(p4ignore_path, gitignore_path)
        return 'copied .p4ignore to .gitignore'

    with open(gitignore_path, 'w') as f:
        pass
    return 'created empty .gitignore'


def init_command(args: argparse.Namespace) -> int:
    """Execute the init command."""
    cwd = os.getcwd()

    log.heading('Checking Perforce workspace')
    workspace_name = _get_p4_workspace_name(cwd)
    if not workspace_name:
        return 1

    log.heading('Checking clobber flag')
    if _check_clobber(cwd):
        log.info('clobber is enabled')
    else:
        log.error(
            f'clobber is not enabled on workspace "{workspace_name}".\n'
            '  Git removes read-only flags when switching branches, so p4 sync\n'
            '  will fail to overwrite those files unless clobber is enabled.\n'
            f'  Edit "{workspace_name}" in P4V to set the clobber flag.')
        return 1

    git_dir = os.path.join(cwd, '.git')
    existing_repo = os.path.exists(git_dir)

    if not existing_repo:
        log.heading('Initializing git repository')
        run_with_output(['git', 'init'], cwd=cwd)

    log.heading('Setting up .gitignore')
    result = _setup_gitignore(cwd)
    log.success(result)

    if not existing_repo:
        log.heading('Creating initial commit')
        run_with_output(['git', 'add', '.gitignore'], cwd=cwd)
        run_with_output(
            ['git', 'commit', '-m', 'Initialize git-p4son repository'],
            cwd=cwd)

        log.heading('Next steps')
        log.info('Review and edit .gitignore before adding workspace files.')
        log.info('Then run: git p4son sync')

    return 0
