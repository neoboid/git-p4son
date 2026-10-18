"""Shared test helpers for git_p4son tests."""

import argparse
import atexit
import contextlib
import copy
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Callable, Iterable, Iterator
from datetime import timedelta
from typing import IO, Any, TypeVar, cast, overload
from unittest import mock

from git_p4son import CONFIG_DIR
from git_p4son.backend import open_backend
from git_p4son.changelist_store import find_record, Record
from git_p4son.common import CommandError, RunError, RunResult
from git_p4son.config import ensure_config_dir
from git_p4son.git import EOL_FOR_AUTOCRLF, format_sync_subject, get_autocrlf
from git_p4son.perforce import P4Change, P4SyncPreviewFile


@contextlib.contextmanager
def captured_output() -> Iterator[io.StringIO]:
    """Capture what is printed to stdout and stderr together; yields the StringIO it goes to."""
    with contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(out):
        yield out


@overload
def make_run_result(returncode: int = 0, stdout: list[str] | None = None,
                    stderr: list[str] | None = None,
                    elapsed: timedelta | None = None) -> RunResult[list[str]]: ...


@overload
def make_run_result(returncode: int = 0, *, stdout: bytes,
                    stderr: bytes | None = None,
                    elapsed: timedelta | None = None) -> RunResult[bytes]: ...


def make_run_result(returncode: int = 0, stdout: list[str] | bytes | None = None,
                    stderr: list[str] | bytes | None = None,
                    elapsed: timedelta | None = None) -> RunResult[Any]:
    """Build a RunResult for mocking run(), with lines or, like run(text=False), bytes."""
    empty: list[str] | bytes = b'' if isinstance(stdout, bytes) else []
    return RunResult(
        returncode=returncode,
        stdout=stdout if stdout is not None else empty,
        stderr=stderr if stderr is not None else empty,
        elapsed=elapsed,
    )


def preview_update(path: str) -> P4SyncPreviewFile:
    """Preview entry for a file p4 would update."""
    return P4SyncPreviewFile(mode='upd', filepath=path)


def make_changes(*pairs: tuple[int, str]) -> list[P4Change]:
    """Build a change list from (changelist, user) pairs."""
    return [P4Change(change=cl, user=user) for cl, user in pairs]


def cli_args(argv: list[str], **overrides: Any) -> argparse.Namespace:
    """Parse argv with the real parser, then set the workspace and overrides as run_command would.

    An override the command has no argument for raises AttributeError, so tests follow renames."""
    from git_p4son.git_backend import GitBackend
    from git_p4son.cli import create_parser
    args = create_parser().parse_args(argv)
    args.workspace_dir = '/ws'
    args.invocation_dir = None
    args.backend = None
    for name, value in overrides.items():
        if name not in ('workspace_dir', 'invocation_dir', 'backend') and not hasattr(args, name):
            raise AttributeError(f'{argv[0]} has no argument {name!r}')
        setattr(args, name, value)
    if args.invocation_dir is None:
        args.invocation_dir = args.workspace_dir
    if args.backend is None:
        args.backend = GitBackend(args.workspace_dir)
    if hasattr(args, 'base_branch'):
        args.base_given = args.base_branch
        args.base_branch = args.backend.resolve_base(args.base_branch)
    return args


def start_patch(test: unittest.TestCase, target: str, **kwargs: Any) -> Any:
    """Patch target until test ends; returns the mock."""
    patcher = mock.patch(target, **kwargs)
    mocked = patcher.start()
    test.addCleanup(patcher.stop)
    return mocked


_TestClass = TypeVar('_TestClass', bound=type[unittest.TestCase])
_GitRepoTest = TypeVar('_GitRepoTest', bound='GitRepoTestCase')


def _requires(name: str, required: bool) -> Callable[[_TestClass], _TestClass]:
    """Skip a test class when the program name is not installed, unless required, as CI requires it, to fail instead."""
    def decorate(test_class: _TestClass) -> _TestClass:
        if shutil.which(name) or required:
            return test_class
        return unittest.skip(f'{name} is not installed')(test_class)
    return decorate


def requires_jj(test_class: _TestClass) -> _TestClass:
    """Skip test_class when jj is not installed, unless GIT_P4SON_REQUIRE_JJ is set."""
    return _requires('jj', bool(os.environ.get('GIT_P4SON_REQUIRE_JJ')))(test_class)


def requires_shell(name: str) -> Callable[[_TestClass], _TestClass]:
    """Skip a test class when the shell is not installed, unless GIT_P4SON_REQUIRE_SHELLS names it."""
    return _requires(name, name in os.environ.get('GIT_P4SON_REQUIRE_SHELLS', '').split())


def recorded(changelist: str, workspace_dir: str) -> Record:
    """The record an alias of changelist holds, failing the test if none does."""
    found = find_record(changelist, workspace_dir)
    assert found is not None, f'no alias of CL {changelist} holds a record'
    return found[1]


def isolate_jj(test: unittest.TestCase, workspace_dir: str) -> None:
    """Give jj a fixed user and no user config for the rest of the test."""
    config = os.path.join(
        workspace_dir, '..', f'{os.path.basename(workspace_dir)}-jj.toml')
    open(config, 'w').close()
    test.addCleanup(os.remove, config)
    patcher = mock.patch.dict(os.environ, {'JJ_USER': 'Test', 'JJ_EMAIL': 'test@example.com',
                                           'JJ_CONFIG': config})
    patcher.start()
    test.addCleanup(patcher.stop)


def run_jj(workspace_dir: str, *args: str) -> str:
    """Run jj in workspace_dir and return its stripped stdout."""
    return subprocess.run(['jj', '--no-pager', '--color', 'never', *args], cwd=workspace_dir,
                          capture_output=True, text=True, check=True).stdout.strip()


def init_git_repo(path: str) -> None:
    """Make path a git repository on branch main, with a committer configured and no automatic maintenance."""
    # No background maintenance after commits: it races with copying a built workspace.
    for args in (['init', '-q', '-b', 'main'],
                 ['config', 'user.email', 'test@example.com'],
                 ['config', 'user.name', 'Test'],
                 ['config', 'maintenance.auto', 'false'],
                 ['config', 'gc.auto', '0']):
        subprocess.run(['git', *args], cwd=path, capture_output=True,
                       check=True)


# The workspaces build_workspace built, by the class defining it: (directory, the attributes it set).
_built_workspaces: dict[type, tuple[str, dict[str, Any]]] = {}


class WorkspaceTestCase(unittest.TestCase):
    """A temporary workspace directory for each test, at self.ws.

    A subclass fills it in build_workspace, which runs once per class; each test starts from a copy."""

    # The backend run_command opens, as --backend would choose it; None follows core.backend.
    backend_name: str | None = None

    def setUp(self) -> None:
        super().setUp()
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.ws = tempdir.name
        built = self._built_workspace()
        if built is not None:
            built_dir, attributes = built
            shutil.copytree(built_dir, self.ws, symlinks=True,
                            dirs_exist_ok=True)
            for name, value in copy.deepcopy(attributes).items():
                setattr(self, name, value)
            self.adopt_workspace()

    def build_workspace(self) -> None:
        """Fill self.ws with what each test starts from; each test gets a copy of the attributes it sets."""

    def adopt_workspace(self) -> None:
        """Point what build_workspace set up at this test's copy of the workspace."""

    def _built_workspace(self) -> tuple[str, dict[str, Any]] | None:
        owner = next(c for c in type(self).__mro__
                     if 'build_workspace' in vars(c))
        if owner is WorkspaceTestCase:
            return None
        if owner not in _built_workspaces:
            tempdir = tempfile.TemporaryDirectory()
            atexit.register(tempdir.cleanup)
            # A test of its own, so the patches build_workspace starts end with the build.
            builder = type(self)(self._testMethodName)
            builder.ws = tempdir.name
            before = set(vars(builder))
            try:
                builder.build_workspace()
            finally:
                builder.doCleanups()
            _built_workspaces[owner] = (tempdir.name, {
                name: value for name, value in vars(builder).items()
                if name not in before})
        return _built_workspaces[owner]

    def write_file(self, rel_path: str, content: str = '') -> str:
        """Write a file under the workspace, creating its directories; returns its path."""
        path = os.path.join(self.ws, rel_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(content)
        return path

    def write_config(self, text: str) -> None:
        """Write the workspace's .git-p4son/config.toml, with the folder's .gitignore as git-p4son writes it."""
        ensure_config_dir(self.ws)
        self.write_file(os.path.join(CONFIG_DIR, 'config.toml'), text)

    @contextlib.contextmanager
    def command_args(self, argv: list[str], **overrides: Any) -> Iterator[argparse.Namespace]:
        """Yield cli_args(argv, **overrides) for this workspace, with the backend open as run_command opens it."""
        from git_p4son.cli import create_parser
        snapshot = getattr(create_parser().parse_args(argv), 'snapshot', True)
        with open_backend(self.ws, self.backend_name, snapshot=snapshot) as backend:
            yield cli_args(argv, workspace_dir=self.ws, backend=backend, **overrides)

    def run_command(self, command: Callable[[argparse.Namespace], int], argv: list[str],
                    **overrides: Any) -> str:
        """Run command with command_args(argv, **overrides); returns everything it printed."""
        with captured_output() as out, self.command_args(argv, **overrides) as args:
            self.assertEqual(command(args), 0)
        return out.getvalue()

    def command_fails(self, command: Callable[[argparse.Namespace], int], argv: list[str],
                      message: str) -> None:
        """Run command with command_args(argv), expecting a CommandError matching message."""
        with captured_output(), self.assertRaisesRegex(CommandError, message), \
                self.command_args(argv) as args:
            command(args)


class GitRepoTestCase(WorkspaceTestCase):
    """A temporary git repository on branch main for each test, at self.ws."""

    def build_workspace(self) -> None:
        self.init_repo()

    def init_repo(self) -> None:
        """Make self.ws a git repository."""
        init_git_repo(self.ws)

    def git(self, *args: str, check: bool = True) -> str:
        """Run git in the repository and return its stripped stdout."""
        return subprocess.run(['git', *args], cwd=self.ws, capture_output=True,
                              text=True, check=check).stdout.strip()

    def commit_history(self, *commits: tuple[str, dict[str, str | None]]) -> list[str]:
        """Commit each (message, {path: content, or None to delete}) on top of HEAD; returns the new commits.

        One git fast-import writes them all, then the index and working tree are reset to the last."""
        with open(os.path.join(self.ws, '.git', 'HEAD')) as f:
            ref = f.read().split()[-1]
        parent = self.git('rev-parse', '-q', '--verify', 'HEAD', check=False)
        stream: list[bytes] = []

        def data(text: str) -> None:
            encoded = text.encode()
            stream.append(b'data %d\n' % len(encoded) + encoded + b'\n')

        for mark, (message, files) in enumerate(commits, 1):
            stream.append(f'commit {ref}\nmark :{mark}\n'
                          f'committer Test <test@example.com> {mark} +0000\n'.encode())
            data(message)
            if mark == 1 and parent:
                stream.append(f'from {parent}\n'.encode())
            for path, content in files.items():
                # fast-import reads C-style quoted paths.
                quoted = '"' + path.replace('\\', '\\\\').replace('"', '\\"').replace(
                    '\t', '\\t').replace('\n', '\\n') + '"'
                if content is None:
                    stream.append(f'D {quoted}\n'.encode())
                else:
                    stream.append(f'M 100644 inline {quoted}\n'.encode())
                    data(content)
        with tempfile.TemporaryDirectory() as tempdir:
            marks = os.path.join(tempdir, 'marks')
            subprocess.run(['git', 'fast-import', '--quiet', f'--export-marks={marks}'],
                           cwd=self.ws, input=b''.join(stream), check=True)
            with open(marks) as f:
                commit_by_mark = dict(line.split() for line in f)
        self.git('reset', '-q', '--hard')
        return [commit_by_mark[f':{mark}'] for mark in range(1, len(commits) + 1)]

    def commit(self, message: str = 'test commit', date: str | None = None) -> None:
        """Stage everything and commit it, with date as the author date if given."""
        self.git('add', '-A')
        self.git('commit', '-q', '--allow-empty', '-m',
                 message, *([f'--date={date}'] if date else []))

    def assert_at(self, branch: str, commit: str) -> None:
        """Assert HEAD is commit, on branch, or detached for an empty branch."""
        self.assertEqual(self.git('branch', '--show-current'), branch)
        self.assertEqual(self.git('rev-parse', 'HEAD'), commit)


class FakeP4:
    """A scripted Perforce server for the p4 commands git-p4son runs.

    install() puts it in place of perforce.run and run_with_stream_output; an unscripted command fails the test.
    Depot paths are //<client>/<path>, mapped to <workspace>/<path>. opened maps paths to (changelist, action),
    pending changelist numbers to their description lines and shelved changelist numbers to {path: content}.
    Like run(), it carries out nothing on a dry run."""

    def __init__(self, workspace_dir: str, client: str = 'myclient', user: str = 'me',
                 options: str = 'noallwrite noclobber') -> None:
        self.ws = workspace_dir
        self.client = client
        self.user = user
        self.options = options
        self.changes: dict[int, tuple[str, dict[str, str | None]]] = {}
        self.have = 0
        self.opened: dict[str, tuple[str, str]] = {}
        self.pending: dict[str, list[str]] = {}
        self.shelved: dict[str, dict[str, str | None]] = {}
        self.binary: set[str] = set()
        self.in_workspace = True
        self.calls: list[list[str]] = []

    def install(self, test: unittest.TestCase) -> None:
        start_patch(test, 'git_p4son.perforce.run', side_effect=self.run)
        start_patch(test, 'git_p4son.perforce.run_with_stream_output',
                    side_effect=self.stream)

    def submit(self, change: int, files: dict[str, str | None], user: str = 'other') -> None:
        """Submit files ({path: content, or None to delete}) as change."""
        self.changes[change] = (user, files)

    def files_at(self, change: int) -> dict[str, str]:
        """The depot's {path: content} at change."""
        files: dict[str, str] = {}
        for number in sorted(c for c in self.changes if c <= change):
            for path, content in self.changes[number][1].items():
                if content is None:
                    files.pop(path, None)
                else:
                    files[path] = content
        return files

    def sync_to(self, change: int) -> None:
        """Sync the workspace to change outside git-p4son, as the setup of a test."""
        self._apply(change, preview=False)

    def synced(self) -> list[int]:
        """The changelists synced for real, in order."""
        return [int(args[-1].rsplit('@', 1)[1]) for args in self.calls
                if args[0] == 'sync' and '-n' not in args]

    def _local(self, path: str) -> str:
        return os.path.join(self.ws, *path.split('/'))

    def _depot(self, path: str) -> str:
        return f'//{self.client}/{path}'

    def run(self, command: list[str], cwd: str = '.', dry_run: bool = False,
            input: str | None = None, env: dict[str, str] | None = None,
            text: bool = True, fail_on_returncode: bool = True) -> RunResult[list[str]]:
        args = command[3:]
        if dry_run:
            return RunResult(0, [], [])
        self.calls.append(args)
        files = [self._rel(line) for line in (input or '').splitlines()]
        stderr: list[str] = []
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
        elif args[:2] == ['change', '-o']:
            out = self._spec(args[2])
        elif args[:2] == ['change', '-i']:
            out = [self._save_spec(input or '')]
        elif args[:4] == ['-ztag', 'fstat', '-Ro', '-e']:
            out = self._fstat_opened(
                p for p, (change, _) in self.opened.items() if change == args[4])
        elif args[:4] == ['-ztag', 'fstat', '-Ro', '-Op']:
            out = self._fstat_opened(self.opened)
        elif args[:5] == ['-x', '-', '-ztag', 'fstat', '-Ro']:
            out = self._fstat_opened(p for p in files if p in self.opened)
        elif args[:4] == ['-x', '-', '-ztag', 'fstat']:
            out = []
            for path in files:
                kind = 'binary' if path in self.binary else 'text'
                out += [f'... clientFile {self._local(path)}',
                        f'... headType {kind}', '']
        elif args[:2] == ['-x', '-'] and args[2] in ('add', 'edit', 'delete'):
            out, stderr = self._open(files, args[2], args[4])
        elif args[:3] == ['-x', '-', 'reopen']:
            out = []
            for path in files:
                self.opened[path] = (args[4], self.opened[path][1])
                out.append(
                    f'{self._depot(path)}#1 - reopened; change {args[4]}')
        elif args[:4] == ['-x', '-', 'revert', '-a']:
            out = self._revert_unchanged(files, args[-1], preview='-n' in args)
        elif args[:3] == ['-x', '-', 'revert']:
            out = self._revert(files)
        elif args[0] == 'shelve':
            change = args[-1]
            self.shelved[change] = {
                path: self._content(path) for path, (c, _) in self.opened.items()
                if c == change}
            out = [f'Change {change} files shelved.']
        else:
            raise AssertionError(f'unscripted p4 command: {args}')
        return RunResult(0, out, stderr)

    def _rel(self, path: str) -> str:
        return os.path.relpath(os.path.join(self.ws, path),
                               self.ws).replace(os.sep, '/')

    def _content(self, path: str) -> str | None:
        local = self._local(path)
        if not os.path.exists(local):
            return None
        with open(local) as f:
            return f.read()

    def _fstat_opened(self, paths: Iterable[str]) -> list[str]:
        out = []
        for path in paths:
            change, action = self.opened[path]
            out += [f'... path {self._local(path)}', f'... action {action}',
                    f'... change {change}', '']
        return out

    def _spec(self, change: str) -> list[str]:
        """A pending changelist's spec, as p4 change -o prints it."""
        return ([f'Change:\t{change}', '', f'Client:\t{self.client}', '',
                 f'User:\t{self.user}', '', 'Status:\tpending', '',
                 'Description:']
                + [f'\t{line}' for line in self.pending[change]]
                + ['', 'Files:']
                + [f'\t{self._depot(p)}\t# {a}' for p, (c, a) in self.opened.items()
                   if c == change])

    def _save_spec(self, spec: str) -> str:
        """Create or update a pending changelist from a spec; returns p4's message."""
        lines = spec.splitlines()
        change = next(line.split()[-1] for line in lines
                      if line.startswith('Change:'))
        start = lines.index('Description:') + 1
        description = []
        for line in lines[start:]:
            if not line.startswith('\t'):
                break
            description.append(line[1:])
        if change == 'new':
            change = str(max([*self.changes, *map(int, self.pending), 0]) + 1)
            self.pending[change] = description
            return f'Change {change} created.'
        self.pending[change] = description
        return f'Change {change} updated.'

    def _open(self, paths: list[str], action: str, change: str) -> tuple[list[str], list[str]]:
        """Open paths for action in change; returns (stdout, stderr) as p4 prints them."""
        out: list[str] = []
        stderr: list[str] = []
        in_depot = self.files_at(self.have)
        for path in paths:
            if (action == 'add') == (path in in_depot):
                stderr.append(f'{self._depot(path)} - can\'t {action} '
                              + ('existing file' if action == 'add' else '(not in depot)'))
                continue
            self.opened[path] = (change, action)
            local = self._local(path)
            if action == 'edit':
                os.chmod(local, stat.S_IRUSR | stat.S_IWUSR)
            elif action == 'delete' and os.path.exists(local):
                os.chmod(local, stat.S_IRUSR | stat.S_IWUSR)
                os.remove(local)
            out.append(f'{self._depot(path)}#1 - opened for {action}')
        return out, stderr

    def _revert(self, paths: list[str]) -> list[str]:
        """Revert paths, writing the have revision to disk for the ones in the depot."""
        out = []
        in_depot = self.files_at(self.have)
        for path in paths:
            _, action = self.opened.pop(path)
            if path in in_depot:
                local = self._local(path)
                if os.path.exists(local):
                    os.chmod(local, stat.S_IRUSR | stat.S_IWUSR)
                with open(local, 'w') as f:
                    f.write(in_depot[path])
                os.chmod(local, stat.S_IRUSR)
            out.append(f'{self._depot(path)}#1 - was {action}, reverted')
        return out

    def _revert_unchanged(self, paths: list[str], change: str, preview: bool) -> list[str]:
        """Revert the edits matching the depot and the adds missing on disk, as revert -a does."""
        in_depot = self.files_at(self.have)
        unchanged = []
        for path in paths:
            opened_change, action = self.opened.get(path, (None, None))
            if opened_change != change:
                continue
            if (action == 'edit' and self._content(path) == in_depot[path]
                    or action == 'add' and self._content(path) is None):
                unchanged.append(path)
        if preview:
            return [f'{self._depot(p)}#1 - was {self.opened[p][1]}, reverted'
                    for p in unchanged]
        return self._revert(unchanged)

    def stream(self, command: list[str], on_output: Callable[[str, IO[str]], None],
               cwd: str = '.', keep_stdout: bool = True) -> RunResult[list[str]]:
        args = command[3:]
        self.calls.append(args)
        if args[0] != 'sync':
            raise AssertionError(f'unscripted streamed p4 command: {args}')
        change = int(args[-1].rsplit('@', 1)[1])
        lines, refused = self._apply(change, preview='-n' in args)
        # As p4 prints them: progress on stdout, clobber refusals on stderr.
        for line in lines:
            on_output(line, sys.stdout)
        for line in refused:
            on_output(line, sys.stderr)
        if refused:
            raise RunError(' '.join(command), returncode=1, stderr=refused)
        return RunResult(0, lines if keep_stdout else [], [])

    def _apply(self, change: int, preview: bool) -> tuple[list[str], list[str]]:
        """Sync the workspace from the have-list to change; returns (output lines, clobber refusals)."""
        before, after = self.files_at(self.have), self.files_at(change)
        lines: list[str] = []
        refused: list[str] = []
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


class P4RepoTestCase(GitRepoTestCase):
    """A git repository in the workspace of a scripted Perforce depot, at self.p4.

    The depot has CL 100 (a.txt, b.txt), which the workspace is synced to and main has committed."""

    def build_workspace(self) -> None:
        super().build_workspace()
        self.p4 = FakeP4(self.ws)
        self.p4.install(self)
        self.commit_sync(100, {'a.txt': 'a1\n', 'b.txt': 'b1\n'})

    def commit_sync(self, change: int, files: dict[str, str | None]) -> None:
        """Submit files as change, sync the workspace to it and commit the sync, as sync would."""
        self.p4.submit(change, files)
        self.p4.sync_to(change)
        self.commit(format_sync_subject('//myclient', change))

    def shelves(self) -> list[list[str]]:
        """The p4 shelve calls made so far."""
        return [call for call in self.p4.calls if call[0] == 'shelve']

    def adopt_workspace(self) -> None:
        super().adopt_workspace()
        self.p4.ws = self.ws
        self.p4.calls.clear()
        self.p4.install(self)

    def commit_feature(self) -> None:
        """On branch feature, commit an edit of a.txt, then the delete of b.txt and the add of c.txt."""
        self.git('switch', '-q', '-c', 'feature')
        self.edit('a.txt', 'a2\n')
        self.commit('Change a')
        self.replace_b_with_c()
        self.commit('Replace b with c')

    def replace_b_with_c(self) -> None:
        """Delete b.txt and add c.txt."""
        # Synced files are read-only, and Windows refuses to delete a read-only file.
        os.chmod(os.path.join(self.ws, 'b.txt'), stat.S_IRUSR | stat.S_IWUSR)
        os.remove(os.path.join(self.ws, 'b.txt'))
        self.write_file('c.txt', 'c1\n')

    def hook(self, name: str, body: str) -> None:
        """Add a Python hook script under .git-p4son/hooks/<name>/."""
        path = self.write_file(os.path.join('.git-p4son', 'hooks', name, 'hook.py'),
                               f'#!{sys.executable}\n{body}')
        os.chmod(path, stat.S_IRWXU)

    def read(self, path: str) -> str:
        with open(os.path.join(self.ws, path)) as f:
            return f.read()

    def edit(self, path: str, content: str) -> None:
        """Change a file as the user would, making it writable first."""
        local = os.path.join(self.ws, path)
        if os.path.exists(local):
            os.chmod(local, stat.S_IRUSR | stat.S_IWUSR)
        self.write_file(path, content)


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


class JjTestCase(GitRepoTestCase):
    """A git repository that is also a colocated jj workspace, with its commands run on jj; mixed in before tests.

    Skipped without jj, once requires_jj or on_jj applies."""

    backend_name = 'jj'

    def init_repo(self) -> None:
        super().init_repo()
        isolate_jj(self, self.ws)
        run_jj(self.ws, 'git', 'init', '--colocate')
        # Git for Windows sets core.autocrlf, so jj has to convert line endings as git does to agree with its commits.
        eol_conversion = EOL_FOR_AUTOCRLF[get_autocrlf(self.ws)]
        if eol_conversion != 'none':
            run_jj(self.ws, 'config', 'set', '--repo',
                   'working-copy.eol-conversion', eol_conversion)

    def setUp(self) -> None:
        super().setUp()
        isolate_jj(self, self.ws)

    def jj(self, *args: str) -> str:
        """Run jj in the workspace and return its stripped stdout."""
        return run_jj(self.ws, *args)

    def assert_at(self, branch: str, commit: str) -> None:
        """Assert git's HEAD is commit, where jj keeps it detached at the working change's parent, as branch is."""
        self.assertEqual(self.git('rev-parse', 'HEAD'), commit)
        if branch:
            self.assertEqual(self.git('rev-parse', branch), commit)


def on_jj(test_class: type[_GitRepoTest], skip: dict[str, str] | None = None) -> type[_GitRepoTest]:
    """A copy of test_class run as a JjTestCase, skipped without jj; skip maps git-only tests to the reason."""

    def build_workspace(self: _GitRepoTest) -> None:
        # Defined here, so the copy builds and caches a workspace of its own.
        test_class.build_workspace(self)

    def skipped(reason: str) -> Callable[[_GitRepoTest], None]:
        return unittest.skip(reason)(lambda self: None)

    # type() builds the class, as a class statement can't take test_class as its base for mypy.
    copy = cast(type[_GitRepoTest], type(f'{test_class.__name__}OnJj', (JjTestCase, test_class), {
        'build_workspace': build_workspace, '__module__': test_class.__module__,
        **{name: skipped(reason) for name, reason in (skip or {}).items()}}))
    return requires_jj(copy)
