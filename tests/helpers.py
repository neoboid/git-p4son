"""Shared test helpers for git_p4son tests."""

import atexit
import contextlib
import copy
import io
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from git_p4son import CONFIG_DIR
from git_p4son.common import CommandError, RunError, RunResult
from git_p4son.config import ensure_config_dir
from git_p4son.git import format_sync_subject
from git_p4son.perforce import P4Change, P4SyncPreviewFile


@contextlib.contextmanager
def captured_output():
    """Capture what is printed to stdout and stderr together; yields the StringIO it goes to."""
    with contextlib.redirect_stdout(io.StringIO()) as out, \
            contextlib.redirect_stderr(out):
        yield out


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
    from git_p4son.git_backend import GitBackend
    from git_p4son.cli import create_parser
    args = create_parser().parse_args(argv)
    args.workspace_dir = '/ws'
    args.invocation_dir = None
    args.jj_workspace = None
    args.backend = None
    for name, value in overrides.items():
        if name not in ('workspace_dir', 'invocation_dir', 'jj_workspace', 'backend') and not hasattr(args, name):
            raise AttributeError(f'{argv[0]} has no argument {name!r}')
        setattr(args, name, value)
    if args.invocation_dir is None:
        args.invocation_dir = args.workspace_dir
    if args.backend is None:
        args.backend = GitBackend(args.workspace_dir)
    return args


def start_patch(test, target, **kwargs):
    """Patch target until test ends; returns the mock."""
    patcher = mock.patch(target, **kwargs)
    mocked = patcher.start()
    test.addCleanup(patcher.stop)
    return mocked


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
_built_workspaces = {}


class WorkspaceTestCase(unittest.TestCase):
    """A temporary workspace directory for each test, at self.ws.

    A subclass fills it in build_workspace, which runs once per class; each test starts from a copy."""

    def setUp(self):
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

    def build_workspace(self):
        """Fill self.ws with what each test starts from; each test gets a copy of the attributes it sets."""

    def adopt_workspace(self):
        """Point what build_workspace set up at this test's copy of the workspace."""

    def _built_workspace(self):
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

    def run_command(self, command, argv, **overrides):
        """Run command with cli_args(argv, **overrides); returns everything it printed."""
        with captured_output() as out:
            self.assertEqual(
                command(cli_args(argv, workspace_dir=self.ws, **overrides)), 0)
        return out.getvalue()

    def command_fails(self, command, argv, message):
        """Run command with cli_args(argv), expecting a CommandError matching message."""
        with captured_output(), self.assertRaisesRegex(CommandError, message):
            command(cli_args(argv, workspace_dir=self.ws))


class GitRepoTestCase(WorkspaceTestCase):
    """A temporary git repository on branch main for each test, at self.ws."""

    def build_workspace(self):
        init_git_repo(self.ws)

    def git(self, *args, check=True):
        """Run git in the repository and return its stripped stdout."""
        return subprocess.run(['git', *args], cwd=self.ws, capture_output=True,
                              text=True, check=check).stdout.strip()

    def commit_history(self, *commits):
        """Commit each (message, {path: content, or None to delete}) on top of HEAD; returns the new commits.

        One git fast-import writes them all, then the index and working tree are reset to the last."""
        with open(os.path.join(self.ws, '.git', 'HEAD')) as f:
            ref = f.read().split()[-1]
        parent = self.git('rev-parse', '-q', '--verify', 'HEAD', check=False)
        stream = []

        def data(text):
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

    def commit(self, message='test commit'):
        """Stage everything and commit it."""
        self.git('add', '-A')
        self.git('commit', '-q', '--allow-empty', '-m', message)

    def assert_at(self, branch, commit):
        """Assert HEAD is commit, on branch, or detached for an empty branch."""
        self.assertEqual(self.git('branch', '--show-current'), branch)
        self.assertEqual(self.git('rev-parse', 'HEAD'), commit)


class FakeP4:
    """A scripted Perforce server for the p4 commands git-p4son runs.

    install() puts it in place of perforce.run and run_with_stream_output; an unscripted command fails the test.
    Depot paths are //<client>/<path>, mapped to <workspace>/<path>. opened maps paths to (changelist, action),
    pending changelist numbers to their description lines and shelved changelist numbers to {path: content}.
    Like run(), it carries out nothing on a dry run."""

    def __init__(self, workspace_dir, client='myclient', user='me',
                 options='noallwrite noclobber'):
        self.ws = workspace_dir
        self.client = client
        self.user = user
        self.options = options
        self.changes = {}
        self.have = 0
        self.opened = {}
        self.pending = {}
        self.shelved = {}
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
        if dry_run:
            return RunResult(0, [], [])
        self.calls.append(args)
        files = [self._rel(line) for line in (input or '').splitlines()]
        stderr = []
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
            out = [self._save_spec(input)]
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

    def _rel(self, path):
        return os.path.relpath(os.path.join(self.ws, path),
                               self.ws).replace(os.sep, '/')

    def _content(self, path):
        local = self._local(path)
        if not os.path.exists(local):
            return None
        with open(local) as f:
            return f.read()

    def _fstat_opened(self, paths):
        out = []
        for path in paths:
            change, action = self.opened[path]
            out += [f'... path {self._local(path)}', f'... action {action}',
                    f'... change {change}', '']
        return out

    def _spec(self, change):
        """A pending changelist's spec, as p4 change -o prints it."""
        return ([f'Change:\t{change}', '', f'Client:\t{self.client}', '',
                 f'User:\t{self.user}', '', 'Status:\tpending', '',
                 'Description:']
                + [f'\t{line}' for line in self.pending[change]]
                + ['', 'Files:']
                + [f'\t{self._depot(p)}\t# {a}' for p, (c, a) in self.opened.items()
                   if c == change])

    def _save_spec(self, spec):
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

    def _open(self, paths, action, change):
        """Open paths for action in change; returns (stdout, stderr) as p4 prints them."""
        out, stderr = [], []
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

    def _revert(self, paths):
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

    def _revert_unchanged(self, paths, change, preview):
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


class P4RepoTestCase(GitRepoTestCase):
    """A git repository in the workspace of a scripted Perforce depot, at self.p4.

    The depot has CL 100 (a.txt, b.txt), which the workspace is synced to and main has committed."""

    def build_workspace(self):
        super().build_workspace()
        self.p4 = FakeP4(self.ws)
        self.p4.install(self)
        self.commit_sync(100, {'a.txt': 'a1\n', 'b.txt': 'b1\n'})

    def commit_sync(self, change, files):
        """Submit files as change, sync the workspace to it and commit the sync, as sync would."""
        self.p4.submit(change, files)
        self.p4.sync_to(change)
        self.commit(format_sync_subject('//myclient', change))

    def shelves(self):
        """The p4 shelve calls made so far."""
        return [call for call in self.p4.calls if call[0] == 'shelve']

    def adopt_workspace(self):
        super().adopt_workspace()
        self.p4.ws = self.ws
        self.p4.calls.clear()
        self.p4.install(self)

    def commit_feature(self):
        """On branch feature, commit an edit of a.txt, then the delete of b.txt and the add of c.txt."""
        self.git('switch', '-q', '-c', 'feature')
        self.edit('a.txt', 'a2\n')
        self.commit('Change a')
        os.remove(os.path.join(self.ws, 'b.txt'))
        self.write_file('c.txt', 'c1\n')
        self.commit('Replace b with c')

    def read(self, path):
        with open(os.path.join(self.ws, path)) as f:
            return f.read()

    def edit(self, path, content):
        """Change a file as the user would, making it writable first."""
        local = os.path.join(self.ws, path)
        if os.path.exists(local):
            os.chmod(local, stat.S_IRUSR | stat.S_IWUSR)
        self.write_file(path, content)


def fake_rebase(test, returncode=0):
    """Stand in for git rebase -i in rebase_todo; returns the list each rebase adds (command, editor, todo, message) to.

    The editor is the GIT_SEQUENCE_EDITOR, and the message None when the rebase has no message file."""
    from git_p4son.rebase_todo import MESSAGE_FILE, TODO_FILE
    rebases = []
    real_run = subprocess.run

    def rebase(command, *args, cwd=None, env=None, **kwargs):
        # rebase_todo.subprocess is the subprocess module itself, so pass everything else through.
        if command[:2] != ['git', 'rebase']:
            return real_run(command, *args, cwd=cwd, env=env, **kwargs)
        message_file = os.path.join(cwd, MESSAGE_FILE)
        message = None
        if os.path.exists(message_file):
            with open(message_file) as f:
                message = f.read()
        with open(os.path.join(cwd, TODO_FILE)) as f:
            rebases.append(
                (command, env['GIT_SEQUENCE_EDITOR'], f.read(), message))
        return subprocess.CompletedProcess(command, returncode)
    start_patch(test, 'git_p4son.rebase_todo.subprocess.run',
                side_effect=rebase)
    return rebases


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
