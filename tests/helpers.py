"""Shared test helpers for git_p4son tests."""

import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from git_p4son import CONFIG_DIR
from git_p4son.common import RunError, RunResult
from git_p4son.config import ensure_config_dir
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


def start_patch(test, target, **kwargs):
    """Patch target until test ends; returns the mock."""
    patcher = mock.patch(target, **kwargs)
    mocked = patcher.start()
    test.addCleanup(patcher.stop)
    return mocked


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
        """Write the workspace's .git-p4son/config.toml, with the folder's .gitignore as git-p4son writes it."""
        ensure_config_dir(self.ws)
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


class FakeP4:
    """A scripted Perforce server for the p4 commands git-p4son runs.

    install() puts it in place of perforce.run and run_with_stream_output; an unscripted command fails the test.
    Depot paths are //<client>/<path>, mapped to <workspace>/<path>."""

    def __init__(self, workspace_dir, client='myclient', user='me',
                 options='noallwrite noclobber'):
        self.ws = workspace_dir
        self.client = client
        self.user = user
        self.options = options
        self.changes = {}
        self.have = 0
        self.opened = {}
        self.binary = set()
        self.in_workspace = True
        self.calls = []

    def install(self, test):
        start_patch(test, 'git_p4son.perforce.run', side_effect=self.run)
        start_patch(test, 'git_p4son.perforce.run_with_stream_output',
                    side_effect=self.stream)

    def submit(self, change, files, user='other'):
        """Submit files ({path: content, or None to delete}) as change."""
        self.changes[change] = (user, files)

    def files_at(self, change):
        """The depot's {path: content} at change."""
        files = {}
        for number in sorted(c for c in self.changes if c <= change):
            for path, content in self.changes[number][1].items():
                if content is None:
                    files.pop(path, None)
                else:
                    files[path] = content
        return files

    def sync_to(self, change):
        """Sync the workspace to change outside git-p4son, as the setup of a test."""
        self._apply(change, preview=False)

    def synced(self):
        """The changelists synced for real, in order."""
        return [int(args[-1].rsplit('@', 1)[1]) for args in self.calls
                if args[0] == 'sync' and '-n' not in args]

    def _local(self, path):
        return os.path.join(self.ws, *path.split('/'))

    def _depot(self, path):
        return f'//{self.client}/{path}'

    def run(self, command, cwd='.', dry_run=False, input=None, env=None,
            text=True, fail_on_returncode=True):
        args = command[3:]
        self.calls.append(args)
        if args[:3] == ['-ztag', 'client', '-o']:
            out = [f'... Client {self.client}', f'... Root {self.ws}',
                   f'... Options {self.options}', '... LineEnd local']
            if self.in_workspace:
                out.append('... Update 2026/01/01 00:00:00')
        elif args[:2] == ['-ztag', 'info']:
            out = [f'... userName {self.user}']
        elif args[:2] == ['-ztag', 'users']:
            known = {u.lower(): u for u, _ in self.changes.values()}
            known[self.user.lower()] = self.user
            out = [f'... User {known[n.lower()]}' for n in args[2:]
                   if n.lower() in known]
        elif args[:3] == ['-ztag', 'changes', '-m1']:
            out = [f'... change {max(self.changes)}']
        elif args[:4] == ['-ztag', 'changes', '-s', 'submitted']:
            start, end = (int(n.lstrip('@'))
                          for n in args[4].split('@', 1)[1].split(','))
            out = []
            for number in sorted(c for c in self.changes if start <= c <= end):
                out += [f'... change {number}',
                        f'... user {self.changes[number][0]}', '']
        elif args[:4] == ['-ztag', 'fstat', '-Ro', '-Op']:
            out = []
            for path, action in self.opened.items():
                out += [f'... path {self._local(path)}',
                        f'... action {action}', '']
        elif args[:4] == ['-x', '-', '-ztag', 'fstat']:
            out = []
            for local in (input or '').splitlines():
                rel = os.path.relpath(local, self.ws).replace(os.sep, '/')
                kind = 'binary' if rel in self.binary else 'text'
                out += [f'... clientFile {local}', f'... headType {kind}', '']
        else:
            raise AssertionError(f'unscripted p4 command: {args}')
        return RunResult(0, out, [])

    def stream(self, command, on_output, cwd='.', keep_stdout=True):
        args = command[3:]
        self.calls.append(args)
        if args[0] != 'sync':
            raise AssertionError(f'unscripted streamed p4 command: {args}')
        change = int(args[-1].rsplit('@', 1)[1])
        lines, refused = self._apply(change, preview='-n' in args)
        for line in lines + refused:
            on_output(line, None)
        if refused:
            raise RunError(' '.join(command), returncode=1, stderr=refused)
        return RunResult(0, lines if keep_stdout else [], [])

    def _apply(self, change, preview):
        """Sync the workspace from the have-list to change; returns (output lines, clobber refusals)."""
        before, after = self.files_at(self.have), self.files_at(change)
        lines, refused = [], []
        for path in sorted(set(before) | set(after)):
            if before.get(path) == after.get(path):
                continue
            local = self._local(path)
            if path not in after:
                what = 'deleted as'
            elif path in before:
                what = 'updating'
            else:
                what = 'added as'
            writable = os.path.exists(local) and os.stat(
                local).st_mode & stat.S_IWUSR
            if (not preview and writable and 'noclobber' in self.options
                    and 'allwrite' not in self.options.replace('noallwrite', '')):
                refused.append(f"Can't clobber writable file {local}")
                continue
            lines.append(f'{self._depot(path)}#1 - {what} {local}')
            if preview:
                continue
            if path not in after:
                if os.path.exists(local):
                    os.chmod(local, stat.S_IRUSR | stat.S_IWUSR)
                    os.remove(local)
                continue
            os.makedirs(os.path.dirname(local), exist_ok=True)
            if os.path.exists(local):
                os.chmod(local, stat.S_IRUSR | stat.S_IWUSR)
            with open(local, 'w') as f:
                f.write(after[path])
            if 'allwrite' not in self.options.replace('noallwrite', ''):
                os.chmod(local, stat.S_IRUSR)
        if not preview:
            self.have = change
        if not lines and not refused:
            lines.append(f'//{self.client}/...@{change} - file(s) up-to-date.')
        return lines, refused


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
