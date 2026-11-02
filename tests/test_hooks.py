"""Tests for git_p4son.hooks module."""

import contextlib
import io
import os
import stat
import tempfile
import unittest
from unittest import mock

from git_p4son.hooks import run_hooks
from tests.helpers import captured_output, make_run_result


class TestRunHooks(unittest.TestCase):
    def test_missing_hook_dir_is_noop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch('git_p4son.hooks.run') as mock_run:
                result = run_hooks('post-sync', tmpdir, tmpdir, 'git')
        self.assertEqual(result, [])
        mock_run.assert_not_called()

    @unittest.skipIf(os.name == 'nt', 'POSIX executable bit test')
    @mock.patch('git_p4son.hooks.run')
    def test_posix_runs_executable_and_warns_for_non_executable(
            self, mock_run):
        mock_run.return_value = make_run_result()
        with tempfile.TemporaryDirectory() as tmpdir:
            hook_dir = os.path.join(tmpdir, '.git-p4son', 'hooks', 'post-sync')
            os.makedirs(hook_dir)
            executable = os.path.join(hook_dir, 'run-me')
            skipped = os.path.join(hook_dir, 'skip-me')
            with open(executable, 'w') as f:
                f.write('#!/bin/sh\n')
            with open(skipped, 'w') as f:
                f.write('#!/bin/sh\n')
            os.chmod(executable, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

            with captured_output() as out:
                run_hooks('post-sync', tmpdir,
                          os.path.join(tmpdir, 'subdir'), 'git')

        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.args[0], [executable])
        self.assertEqual(mock_run.call_args.kwargs['cwd'],
                         os.path.join(tmpdir, 'subdir'))
        self.assertEqual(
            mock_run.call_args.kwargs['env'],
            {'GIT_P4SON_REPO_ROOT_DIR': os.path.abspath(tmpdir), 'GIT_P4SON_BACKEND': 'git'})
        self.assertEqual(out.getvalue().count('[warn]'), 1)
        self.assertIn('skip-me', out.getvalue())

    @mock.patch('git_p4son.scripts._is_windows', return_value=True)
    @mock.patch('git_p4son.hooks.run')
    def test_windows_warns_for_unknown_extension(self, mock_run,
                                                 _is_windows):
        with tempfile.TemporaryDirectory() as tmpdir:
            hook_dir = os.path.join(tmpdir, '.git-p4son', 'hooks', 'post-sync')
            os.makedirs(hook_dir)
            with open(os.path.join(hook_dir, 'readme.txt'), 'w') as f:
                f.write('')

            with captured_output() as out:
                run_hooks('post-sync', tmpdir, tmpdir, 'git')

        mock_run.assert_not_called()
        self.assertEqual(out.getvalue().count('[warn]'), 1)

    @mock.patch('git_p4son.scripts._is_windows', return_value=True)
    @mock.patch('git_p4son.hooks.run')
    def test_prints_hook_stdout_after_command_finishes(
            self, mock_run, _is_windows):
        mock_run.return_value = make_run_result(stdout=['hook output'])
        with tempfile.TemporaryDirectory() as tmpdir:
            hook_dir = os.path.join(tmpdir, '.git-p4son', 'hooks', 'post-sync')
            os.makedirs(hook_dir)
            with open(os.path.join(hook_dir, 'script.py'), 'w') as f:
                f.write('')

            with captured_output() as out:
                run_hooks('post-sync', tmpdir, tmpdir, 'git')

        mock_run.assert_called_once()
        self.assertIn('\nhook output\n', out.getvalue())

    @mock.patch('git_p4son.scripts._is_windows', return_value=True)
    @mock.patch('git_p4son.hooks.run')
    def test_failing_hook_does_not_stop_remaining_hooks(
            self, mock_run, _is_windows):
        """One failing hook is reported and the rest still run."""
        mock_run.side_effect = [
            make_run_result(returncode=1, stderr=['boom']),
            make_run_result(),
        ]
        with tempfile.TemporaryDirectory() as tmpdir:
            hook_dir = os.path.join(tmpdir, '.git-p4son', 'hooks',
                                    'post-sync')
            os.makedirs(hook_dir)
            for name in ('a.py', 'b.py'):
                with open(os.path.join(hook_dir, name), 'w') as f:
                    f.write('')

            with captured_output() as out:
                results = run_hooks('post-sync', tmpdir, tmpdir, 'git')

        self.assertEqual(mock_run.call_count, 2)
        self.assertEqual(len(results), 2)
        self.assertEqual(out.getvalue().count('[err]'), 1)

    @mock.patch('git_p4son.scripts._is_windows', return_value=True)
    @mock.patch('git_p4son.hooks.run')
    def test_prints_hook_stderr(self, mock_run, _is_windows):
        mock_run.return_value = make_run_result(stderr=['a warning'])
        with tempfile.TemporaryDirectory() as tmpdir:
            hook_dir = os.path.join(tmpdir, '.git-p4son', 'hooks',
                                    'post-sync')
            os.makedirs(hook_dir)
            with open(os.path.join(hook_dir, 'script.py'), 'w') as f:
                f.write('')

            with contextlib.redirect_stderr(io.StringIO()) as err:
                run_hooks('post-sync', tmpdir, tmpdir, 'git')

        self.assertEqual(err.getvalue(), 'a warning\n')

    @mock.patch('git_p4son.scripts._is_windows', return_value=True)
    @mock.patch('git_p4son.hooks.run')
    def test_hooks_run_in_sorted_order(self, mock_run, _is_windows):
        mock_run.return_value = make_run_result()
        with tempfile.TemporaryDirectory() as tmpdir:
            hook_dir = os.path.join(tmpdir, '.git-p4son', 'hooks', 'post-sync')
            os.makedirs(hook_dir)
            second = os.path.join(hook_dir, 'b.py')
            first = os.path.join(hook_dir, 'a.py')
            for path in (second, first):
                with open(path, 'w') as f:
                    f.write('')

            run_hooks('post-sync', tmpdir, tmpdir, 'git')

        commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertEqual(commands, [
            ['python.exe', first],
            ['python.exe', second],
        ])


if __name__ == '__main__':
    unittest.main()
