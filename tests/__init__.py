"""Tests for git_p4son, run with a global git config of their own, outside any repository a caller set."""

import atexit
import os
import subprocess
import tempfile


def _isolate_global_git_config() -> None:
    """Point git, and jj through it, at an empty global config, with no global ignore or attributes file."""
    directory = tempfile.TemporaryDirectory(prefix='git-p4son-tests-')
    atexit.register(directory.cleanup)
    empty = os.path.join(directory.name, 'empty')
    open(empty, 'w').close()
    config = os.path.join(directory.name, 'gitconfig')
    # Set, not left out: unset, git still reads the ignore and attributes files under XDG_CONFIG_HOME.
    for key in ('core.excludesFile', 'core.attributesFile'):
        subprocess.run(['git', 'config', '--file',
                       config, key, empty], check=True)
    os.environ['GIT_CONFIG_GLOBAL'] = config


def _leave_the_calling_repository() -> None:
    """Drop the variables that tie git to a repository, as a hook in a linked worktree passes GIT_DIR down."""
    names = subprocess.run(['git', 'rev-parse', '--local-env-vars'], check=True, capture_output=True,
                           text=True).stdout.split()
    for name in names:
        os.environ.pop(name, None)


_leave_the_calling_repository()
_isolate_global_git_config()
