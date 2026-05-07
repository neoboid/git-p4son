"""Tests for git_p4son.hooks module."""

import os
import stat
import tempfile
import unittest
from unittest import mock

from git_p4son.hooks import run_hooks
from tests.helpers import make_run_result


class TestRunHooks(unittest.TestCase):
    def test_missing_hook_dir_is_noop(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch('git_p4son.hooks.run') as mock_run:
                result = run_hooks('post-sync', tmpdir, tmpdir)
        self.assertEqual(result, [])
        mock_run.assert_not_called()

    @unittest.skipIf(os.name == 'nt', 'POSIX executable bit test')
    @mock.patch('git_p4son.hooks.log')
    @mock.patch('git_p4son.hooks.run')
    def test_posix_runs_executable_and_warns_for_non_executable(
            self, mock_run, mock_log):
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

            run_hooks('post-sync', tmpdir, os.path.join(tmpdir, 'subdir'))

        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.args[0], [executable])
        self.assertEqual(mock_run.call_args.kwargs['cwd'],
                         os.path.join(tmpdir, 'subdir'))
        self.assertEqual(
            mock_run.call_args.kwargs['env']['GIT_P4SON_REPO_ROOT_DIR'],
            os.path.abspath(tmpdir))
        mock_log.warning.assert_called_once()

    @mock.patch('git_p4son.hooks._is_windows', return_value=True)
    @mock.patch('git_p4son.hooks.run')
    def test_windows_runs_default_script_extensions(self, mock_run,
                                                    _is_windows):
        mock_run.return_value = make_run_result()
        with tempfile.TemporaryDirectory() as tmpdir:
            hook_dir = os.path.join(tmpdir, '.git-p4son', 'hooks', 'post-sync')
            os.makedirs(hook_dir)
            ps1 = os.path.join(hook_dir, 'a.ps1')
            nu = os.path.join(hook_dir, 'b.nu')
            py = os.path.join(hook_dir, 'c.py')
            sh = os.path.join(hook_dir, 'd.sh')
            for path in (ps1, nu, py, sh):
                with open(path, 'w') as f:
                    f.write('')

            run_hooks('post-sync', tmpdir, tmpdir)

        commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertEqual(commands, [
            ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
             '-File', ps1],
            ['nushell.exe', nu],
            ['python.exe', py],
            ['bash.exe', sh],
        ])

    @mock.patch('git_p4son.hooks._is_windows', return_value=True)
    @mock.patch('git_p4son.hooks.run')
    def test_windows_config_can_override_extension_association(
            self, mock_run, _is_windows):
        mock_run.return_value = make_run_result()
        with tempfile.TemporaryDirectory() as tmpdir:
            config_dir = os.path.join(tmpdir, '.git-p4son')
            hook_dir = os.path.join(config_dir, 'hooks', 'post-sync')
            os.makedirs(hook_dir)
            script = os.path.join(hook_dir, 'script.ps1')
            with open(script, 'w') as f:
                f.write('')
            with open(os.path.join(config_dir, 'config.toml'), 'w') as f:
                f.write('[hooks.extension-associations]\n')
                f.write('".ps1" = ["pwsh.exe", "-File"]\n')

            run_hooks('post-sync', tmpdir, tmpdir)

        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.args[0],
                         ['pwsh.exe', '-File', script])

    @mock.patch('git_p4son.hooks._is_windows', return_value=True)
    @mock.patch('git_p4son.hooks.log')
    @mock.patch('git_p4son.hooks.run')
    def test_windows_warns_for_unknown_extension(self, mock_run, mock_log,
                                                 _is_windows):
        with tempfile.TemporaryDirectory() as tmpdir:
            hook_dir = os.path.join(tmpdir, '.git-p4son', 'hooks', 'post-sync')
            os.makedirs(hook_dir)
            with open(os.path.join(hook_dir, 'readme.txt'), 'w') as f:
                f.write('')

            run_hooks('post-sync', tmpdir, tmpdir)

        mock_run.assert_not_called()
        mock_log.warning.assert_called_once()

    @mock.patch('git_p4son.hooks._is_windows', return_value=True)
    @mock.patch('git_p4son.hooks.run')
    def test_hooks_run_in_sorted_order(self, mock_run, _is_windows):
        mock_run.return_value = make_run_result()
        with tempfile.TemporaryDirectory() as tmpdir:
            hook_dir = os.path.join(tmpdir, '.git-p4son', 'hooks', 'post-sync')
            os.makedirs(hook_dir)
            second = os.path.join(hook_dir, 'b.nu')
            first = os.path.join(hook_dir, 'a.nu')
            for path in (second, first):
                with open(path, 'w') as f:
                    f.write('')

            run_hooks('post-sync', tmpdir, tmpdir)

        commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertEqual(commands, [
            ['nushell.exe', first],
            ['nushell.exe', second],
        ])


if __name__ == '__main__':
    unittest.main()
