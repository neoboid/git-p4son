"""Tests for git_p4son.common module."""

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from git_p4son.common import (
    CommandError,
    RunResult,
    branch_to_alias,
    get_current_branch,
    get_workspace_dir,
    is_workspace_dir,
    join_command_line,
    run,
    run_with_output,
)


class TestGetCurrentBranch(unittest.TestCase):
    @mock.patch('subprocess.run')
    def test_returns_branch_name(self, mock_run):
        mock_run.return_value = mock.Mock(returncode=0, stdout='feat/foo\n')
        result = get_current_branch('/ws')
        mock_run.assert_called_once_with(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd='/ws',
            capture_output=True,
            text=True,
        )
        self.assertEqual(result, 'feat/foo')

    @mock.patch('subprocess.run')
    def test_detached_head_returns_none(self, mock_run):
        mock_run.return_value = mock.Mock(returncode=0, stdout='HEAD\n')
        result = get_current_branch('/ws')
        self.assertIsNone(result)

    @mock.patch('subprocess.run')
    def test_command_failure_returns_none(self, mock_run):
        mock_run.return_value = mock.Mock(returncode=128, stdout='')
        result = get_current_branch('/ws')
        self.assertIsNone(result)

    @mock.patch('subprocess.run', side_effect=OSError('no git'))
    def test_exception_returns_none(self, mock_run):
        result = get_current_branch('/ws')
        self.assertIsNone(result)


class TestBranchToAlias(unittest.TestCase):
    def test_replaces_slashes(self):
        self.assertEqual(branch_to_alias('feat/my-feature'), 'feat-my-feature')

    def test_no_slash_passthrough(self):
        self.assertEqual(branch_to_alias('my-branch'), 'my-branch')

    def test_multiple_slashes(self):
        self.assertEqual(branch_to_alias('a/b/c'), 'a-b-c')


class TestJoinCommandLine(unittest.TestCase):
    def test_simple_command(self):
        result = join_command_line(['git', 'status'])
        self.assertEqual(result, ' git status')

    def test_argument_with_spaces_is_quoted(self):
        result = join_command_line(['git', 'commit', '-m', 'hello world'])
        self.assertEqual(result, ' git commit -m "hello world"')

    def test_empty_command(self):
        result = join_command_line([])
        self.assertEqual(result, '')

    def test_single_argument(self):
        result = join_command_line(['ls'])
        self.assertEqual(result, ' ls')


class TestIsWorkspaceDir(unittest.TestCase):
    def test_returns_true_when_git_dir_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, '.git'))
            self.assertTrue(is_workspace_dir(tmpdir))

    def test_returns_false_when_no_git_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertFalse(is_workspace_dir(tmpdir))


class TestGetWorkspaceDir(unittest.TestCase):
    def test_finds_workspace_from_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, '.git'))
            subdir = os.path.join(tmpdir, 'a', 'b')
            os.makedirs(subdir)
            with mock.patch('os.getcwd', return_value=subdir):
                result = get_workspace_dir()
            self.assertEqual(result, tmpdir)

    def test_returns_none_when_no_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch('os.getcwd', return_value=tmpdir):
                result = get_workspace_dir()
            self.assertIsNone(result)


class TestRunResult(unittest.TestCase):
    def test_fields(self):
        r = RunResult(0, ['line1'], ['err1'])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, ['line1'])
        self.assertEqual(r.stderr, ['err1'])


class TestRun(unittest.TestCase):
    @mock.patch('subprocess.run')
    def test_calls_subprocess_and_returns_result(self, mock_subprocess_run):
        mock_subprocess_run.return_value = mock.Mock(
            returncode=0,
            stdout='line1\nline2\n',
            stderr='',
        )
        result = run(['git', 'status'], cwd='/tmp')
        mock_subprocess_run.assert_called_once_with(
            ['git', 'status'],
            cwd='/tmp',
            capture_output=True,
            text=True,
            input=None,
        )
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, ['line1', 'line2'])
        self.assertEqual(result.stderr, [])

    def test_dry_run_returns_empty_result(self):
        result = run(['git', 'status'], dry_run=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, [])
        self.assertEqual(result.stderr, [])

    @mock.patch('subprocess.run')
    def test_nonzero_returncode_raises(self, mock_subprocess_run):
        mock_subprocess_run.return_value = mock.Mock(
            returncode=1,
            stdout='',
            stderr='error\n',
        )
        with self.assertRaises(CommandError) as ctx:
            run(['p4', 'info'])
        self.assertEqual(ctx.exception.returncode, 1)
        self.assertEqual(ctx.exception.stderr, ['error'])


class TestRunWithOutput(unittest.TestCase):
    @mock.patch('subprocess.Popen')
    def test_calls_popen_and_returns_result(self, mock_popen_cls):
        mock_process = mock.MagicMock()
        mock_process.stdout.readline = mock.Mock(side_effect=['out1\n', ''])
        mock_process.stderr.readline = mock.Mock(side_effect=['err1\n', ''])
        mock_process.poll = mock.Mock(side_effect=[None, 0])
        mock_process.returncode = 0
        mock_process.communicate.return_value = ('', '')
        mock_process.__enter__ = mock.Mock(return_value=mock_process)
        mock_process.__exit__ = mock.Mock(return_value=False)
        mock_popen_cls.return_value = mock_process

        result = run_with_output(['git', 'status'], cwd='/tmp')
        mock_popen_cls.assert_called_once_with(
            ['git', 'status'],
            cwd='/tmp',
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=None,
            text=True,
        )
        self.assertEqual(result.returncode, 0)

    @mock.patch('subprocess.Popen')
    def test_callback_is_invoked(self, mock_popen_cls):
        mock_process = mock.MagicMock()
        mock_process.stdout.readline = mock.Mock(side_effect=['hello\n', ''])
        mock_process.stderr.readline = mock.Mock(side_effect=[''])
        mock_process.poll = mock.Mock(side_effect=[None, 0])
        mock_process.returncode = 0
        mock_process.communicate.return_value = ('', '')
        mock_process.__enter__ = mock.Mock(return_value=mock_process)
        mock_process.__exit__ = mock.Mock(return_value=False)
        mock_popen_cls.return_value = mock_process

        callback = mock.Mock()
        run_with_output(['echo', 'hello'], on_output=callback)
        callback_lines = [call.kwargs.get('line', call.args[0] if call.args else None)
                          for call in callback.call_args_list]
        self.assertIn('hello', callback_lines)


if __name__ == '__main__':
    unittest.main()
