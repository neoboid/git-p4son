"""Init command: sets up a git repository inside a Perforce workspace."""

import argparse
import os
import shutil

from . import jj
from .backend import workspace_lock
from .common import CommandError, prompt_choice, prompt_menu, prompt_numbered
from .config import save_config
from .depot import (
    WORKSPACE_DEPOT_ROOT,
    check_depot_root,
    expand_depot_root,
    get_depot_root,
    subdirectory_depot_root,
)
from .log import log
from .perforce import get_client_spec
from .skill import install_skill, is_claude_code_installed, is_skill_installed
from .git import (
    add_files,
    branch_exists,
    commit,
    get_current_branch,
    has_commits,
    init_repo,
    is_workspace_dir,
    list_branches,
)
from .sync_branch import DEFAULT_SYNC_BRANCH, get_sync_branch_setting
from .sync_split_users import current_p4_user, is_self_split, set_self_split
from .writable import is_writable_mode, set_writable_mode


def _select_depot_root(client_name: str, cwd: str,
                       p4_workspace_root: str) -> str | None:
    """Interactively select a depot root template to store, or None to abort."""
    roots = {'entire workspace': WORKSPACE_DEPOT_ROOT}
    cwd_root = subdirectory_depot_root(cwd, p4_workspace_root)
    if cwd_root:
        roots['current directory'] = cwd_root
    options = [(name, f'{expand_depot_root(root, client_name)}/...')
               for name, root in roots.items()] + [('quit', '')]

    while True:
        log.info('')
        choice = prompt_menu('Select the depot path that git-p4son should sync:',
                             options, 'entire workspace')
        if choice is None or choice == 'quit':
            return None
        if check_depot_root(roots[choice], client_name, cwd):
            return roots[choice]


def _configure_depot_root(client_name: str, cwd: str,
                          p4_workspace_root: str) -> None:
    """Configure depot root: validate existing or prompt for new; raise CommandError if none is chosen."""
    log.heading('Finding depot root')
    depot_root = get_depot_root(cwd)
    if depot_root:
        log.success(f'{expand_depot_root(depot_root, client_name)}/...')

        log.heading('Validating depot root')
        if check_depot_root(depot_root, client_name, cwd):
            log.success('Valid')
            return
    else:
        log.warning('No root configured')

    log.heading('Configuring depot root')
    depot_root = _select_depot_root(client_name, cwd, p4_workspace_root)
    if not depot_root:
        raise CommandError('No depot root selected, aborting')

    log.success(f'{expand_depot_root(depot_root, client_name)}/...')
    save_config(cwd, {'depot': {'root': depot_root}})


def _ask_yes_no(question: str, current: bool) -> bool | None:
    """Ask a yes/no question, where an empty answer keeps current; None on EOF."""
    answer = prompt_choice(question, ['yes', 'no'], 'yes' if current else 'no')
    return None if answer is None else answer == 'yes'


def _configure_writable_mode(cwd: str) -> tuple[bool, bool]:
    """Ask for the writable mode setting and save it; returns (enabled, changed)."""
    log.heading('Configuring writable mode')
    current = is_writable_mode(cwd)
    log.info('')
    log.info('Writable mode keeps git-tracked files writable, so they can be')
    log.info('edited without p4 edit. Git-ignored files stay read-only.')
    enabled = _ask_yes_no('Keep git-tracked files writable?', current)
    if enabled is None or enabled == current:
        log.success(f'{"on" if current else "off"} (unchanged)')
        return current, False
    set_writable_mode(cwd, enabled)
    log.success('on' if enabled else 'off')
    return enabled, True


def _configure_split_users(cwd: str) -> None:
    """Ask whether to split out the current user's changelists and save it."""
    log.heading('Configuring split users')
    user = current_p4_user(cwd)
    if not user:
        log.warning('Cannot determine the current Perforce user, skipping')
        return
    current = is_self_split(cwd)
    log.info('')
    log.info('Split users have each of their submitted changelists synced into')
    log.info('its own commit, so your own submits show up in git as separate')
    log.info('commits.')
    enabled = _ask_yes_no(
        f'Sync your own changelists ({user}) individually?', current)
    if enabled is None or enabled == current:
        log.success(f'{"on" if current else "off"} (unchanged)')
    else:
        set_self_split(cwd, enabled)
        log.success('on' if enabled else 'off')
    log.info('Split out other users with: '
             'git p4son sync-split-users add NAME')


def _configure_sync_branch(cwd: str, new_repo: bool) -> None:
    """Record [sync] branch, unless set: HEAD's branch in a new repo, else main, else HEAD's, else one picked."""
    log.heading('Finding sync branch')
    branch = get_sync_branch_setting(cwd)
    if branch is not None:
        log.success(branch)
        return
    head = get_current_branch(cwd)
    # In an existing repo, HEAD may be on a feature branch, so main comes first there.
    if new_repo and head is not None:
        branch = head
    elif branch_exists(DEFAULT_SYNC_BRANCH, cwd):
        branch = DEFAULT_SYNC_BRANCH
    else:
        branch = head
    if branch is None:
        branches = list_branches(cwd)
        branch = prompt_numbered(
            'Select the branch sync commits onto:', branches) if branches else None
    if branch is None:
        log.warning(
            'No sync branch chosen. Set [sync] branch in .git-p4son/config.toml before syncing.')
        return
    save_config(cwd, {'sync': {'branch': branch}})
    log.success(
        f'{branch}, saved as [sync] branch in .git-p4son/config.toml, where you can change it')


def _configure_claude_skill() -> None:
    """Offer to install or refresh the Claude Code skill, if Claude Code is present."""
    log.heading('Configuring Claude Code skill')
    if not is_claude_code_installed():
        log.success('Claude Code not found, skipping')
        return
    if is_skill_installed():
        install_skill()
        log.success('Installed (refreshed)')
        return
    log.info('')
    log.info('The Claude Code skill teaches AI agents how to use git-p4son.')
    log.info('It is installed for your user, not in the workspace.')
    if _ask_yes_no('Install the Claude Code skill?', False):
        log.success(f'Installed to {install_skill()}')
    else:
        log.success('Not installed')


def _configure_jj(cwd: str) -> bool:
    """Offer to colocate jj and to add the jj p4son alias, with a supported jj; returns whether to colocate."""
    log.heading('Configuring jj')
    if not jj.is_supported(cwd):
        log.success(f'jj {jj.MIN_VERSION} or newer not found, skipping')
        return False
    colocate = False
    if jj.is_workspace(cwd):
        log.success('Colocated jj workspace')
    else:
        log.info('')
        log.info('jj can work in this git repository alongside git, colocated.')
        if not _ask_yes_no('Use jj in this workspace too (colocated)?', False):
            log.success('Not colocated')
            return False
        colocate = True
        log.success('Will colocate')
    if jj.has_p4son_alias(cwd):
        log.success('jj p4son alias found')
    elif _ask_yes_no('Add the jj p4son alias to your jj config, to run git-p4son as jj p4son?', True):
        jj.add_p4son_alias(cwd)
        log.success('Added the jj p4son alias')
    else:
        log.success('jj p4son alias not added')
    return colocate


def _setup_gitignore(cwd: str) -> str:
    """Set up .gitignore file. Returns a description of what was done."""
    gitignore_path = os.path.join(cwd, '.gitignore')
    p4ignore_path = os.path.join(cwd, '.p4ignore')

    if os.path.exists(gitignore_path):
        return '.gitignore already exists'

    if os.path.exists(p4ignore_path):
        shutil.copy2(p4ignore_path, gitignore_path)
        return 'Copied .p4ignore to a new .gitignore'

    open(gitignore_path, 'w').close()
    return 'Created an empty .gitignore'


def init_command(args: argparse.Namespace) -> int:
    """Execute the init command."""
    cwd = os.getcwd()
    with workspace_lock(cwd):
        return _init_workspace(cwd)


def _init_workspace(cwd: str) -> int:
    """Set up git-p4son in the Perforce workspace at cwd."""

    log.heading('Checking Perforce workspace')
    spec = get_client_spec(cwd)
    if not spec:
        raise CommandError('Not inside a Perforce workspace. '
                           'Is Perforce installed and configured?')
    log.success(spec.name)

    _configure_depot_root(spec.name, cwd, spec.root)

    writable, writable_changed = _configure_writable_mode(cwd)

    _configure_split_users(cwd)

    _configure_claude_skill()

    colocate = _configure_jj(cwd)

    log.heading('Checking .gitignore')
    log.success(_setup_gitignore(cwd))

    log.heading('Checking git repo')
    if is_workspace_dir(cwd):
        log.success('.git/ already exists')
    else:
        init_repo(cwd)
        log.success('Created a new git repository')

    # Also covers a repo left without commits by an init that failed to commit.
    created = not has_commits(cwd)
    if created:
        log.heading('Creating initial commit')
        add_files(['.gitignore'], cwd)
        commit('Initialize git-p4son repository', cwd)
        log.success('Committed .gitignore')

    # After the first commit, so HEAD is on a branch.
    _configure_sync_branch(cwd, created)

    # A new repo's files are untracked until the user's first commit, and jj would snapshot them all.
    if colocate and not created:
        # Last, so jj imports the branch as a bookmark.
        log.heading('Colocating jj')
        jj.colocate(cwd)
        log.success('Colocated')

    if created:
        log.heading('Next steps')
        log.info('* Review and edit .gitignore')
        log.info('* git add .')
        log.info('* git commit -m "Initial commit"')
        if colocate:
            log.info('* jj git init --colocate')
        log.info('* git p4son sync')
        if writable:
            log.info('* git p4son writable apply')
    elif writable_changed:
        log.heading('Next steps')
        log.info('* git p4son writable apply, to update the tracked files')

    return 0
