"""Shared test helpers for git_p4son tests."""

import os
import subprocess
import tempfile
import unittest

from git_p4son import CONFIG_DIR
from git_p4son.common import RunResult
from git_p4son.perforce import P4Change, P4SyncPreviewFile


def make_run_result(returncode=0, stdout=None, stderr=None, elapsed=None):
    """Build a RunResult for mocking run()."""
    return RunResult(
        returncode=returncode,
        stdout=stdout if stdout is not None else [],
        stderr=stderr if stderr is not None else [],
        elapsed=elapsed,
    )


def preview_update(path):
    """Preview entry for a file p4 would update."""
    return P4SyncPreviewFile(mode='upd', filepath=path)


def make_changes(*pairs):
    """Build a change list from (changelist, user) pairs."""
    return [P4Change(change=cl, user=user) for cl, user in pairs]


def cli_args(argv, **overrides):
    """Parse argv with the real parser, then set the workspace and overrides as run_command would.

    An override the command has no argument for raises AttributeError, so tests follow renames."""
    from git_p4son.cli import create_parser
    args = create_parser().parse_args(argv)
    args.workspace_dir = '/ws'
    args.invocation_dir = None
    for name, value in overrides.items():
        if name not in ('workspace_dir', 'invocation_dir') and not hasattr(args, name):
            raise AttributeError(f'{argv[0]} has no argument {name!r}')
        setattr(args, name, value)
    if args.invocation_dir is None:
        args.invocation_dir = args.workspace_dir
    return args


def init_git_repo(path: str) -> None:
    """Make path a git repository on branch main, with a committer configured."""
    for args in (['init', '-q', '-b', 'main'],
                 ['config', 'user.email', 'test@example.com'],
                 ['config', 'user.name', 'Test']):
        subprocess.run(['git', *args], cwd=path, capture_output=True,
                       check=True)


class WorkspaceTestCase(unittest.TestCase):
    """A temporary workspace directory for each test, at self.ws."""

    def setUp(self):
        super().setUp()
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.ws = tempdir.name

    def write_file(self, rel_path, content=''):
        """Write a file under the workspace, creating its directories; returns its path."""
        path = os.path.join(self.ws, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(content)
        return path

    def write_config(self, text):
        """Write the workspace's .git-p4son/config.toml."""
        self.write_file(os.path.join(CONFIG_DIR, 'config.toml'), text)


class GitRepoTestCase(WorkspaceTestCase):
    """A temporary git repository on branch main for each test, at self.ws."""

    def setUp(self):
        super().setUp()
        init_git_repo(self.ws)

    def git(self, *args, check=True):
        """Run git in the repository and return its stripped stdout."""
        return subprocess.run(['git', *args], cwd=self.ws, capture_output=True,
                              text=True, check=check).stdout.strip()

    def commit(self, message='test commit'):
        """Stage everything and commit it; returns the new commit."""
        self.git('add', '-A')
        self.git('commit', '-q', '--allow-empty', '-m', message)
        return self.git('rev-parse', 'HEAD')


class MockRunDispatcher:
    """Answer run() calls with the result of the first command-prefix match, else the default."""

    def __init__(self, mapping=None, default=None):
        self.mapping = mapping or {}
        self.default = default or make_run_result(
            returncode=1, stderr=['unmatched command'])
        self.calls = []

    def __call__(self, command, cwd='.', dry_run=False, input=None,
                 env=None):
        self.calls.append((command, cwd, dry_run))
        for prefix, result in self.mapping.items():
            if tuple(command[:len(prefix)]) == prefix:
                return result
        return self.default


# A pending changelist spec as p4 change -o prints it.
SAMPLE_SPEC = """\
# A Perforce Change Specification.
Change:\t12345

Client:\tmyclient

User:\tmyuser

Status:\tpending

Description:
\tFix the login bug
\t1. Add validation
\t2. Fix redirect

Files:
\t//depot/src/login.py\t# edit
"""

SAMPLE_SPEC_NO_COMMITS = """\
Change:\tnew

Description:
\tJust a message

Files:
"""
