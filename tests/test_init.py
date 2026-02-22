"""Tests for git_p4son.init module."""

import os
import unittest
from unittest import mock

from git_p4son.common import RunError
from git_p4son.init import (
    _check_clobber,
    _check_p4_workspace,
    _setup_gitignore,
    init_command,
)
from tests.helpers import MockRunDispatcher, make_run_result


class TestCheckP4Workspace(unittest.TestCase):
    @mock.patch('git_p4son.init.run')
    def test_valid_workspace(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=['Client name: my-client', 'Client root: /ws'])
        self.assertTrue(_check_p4_workspace('/ws'))

    @mock.patch('git_p4son.init.run')
    def test_unknown_client(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=['Client name: *unknown*'])
        self.assertFalse(_check_p4_workspace('/ws'))

    @mock.patch('git_p4son.init.run')
    def test_no_client_line(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=['Server address: ssl:perforce:1666'])
        self.assertFalse(_check_p4_workspace('/ws'))

    @mock.patch('git_p4son.init.run', side_effect=RunError('p4 info', returncode=1))
    def test_p4_failure(self, mock_run):
        self.assertFalse(_check_p4_workspace('/ws'))


class TestCheckClobber(unittest.TestCase):
    @mock.patch('git_p4son.init.run')
    def test_clobber_enabled(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=['Options:\tallwrite clobber compress'])
        self.assertTrue(_check_clobber('/ws'))

    @mock.patch('git_p4son.init.run')
    def test_noclobber(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=['Options:\tallwrite noclobber compress'])
        self.assertFalse(_check_clobber('/ws'))

    @mock.patch('git_p4son.init.run')
    def test_no_options_line(self, mock_run):
        mock_run.return_value = make_run_result(stdout=['Root:\t/ws'])
        self.assertFalse(_check_clobber('/ws'))

    @mock.patch('git_p4son.init.run', side_effect=RunError('p4 client', returncode=1))
    def test_p4_failure(self, mock_run):
        self.assertFalse(_check_clobber('/ws'))


class TestSetupGitignore(unittest.TestCase):
    def test_existing_gitignore_left_as_is(self):
        with mock.patch('os.path.exists') as mock_exists:
            mock_exists.side_effect = lambda p: p.endswith('.gitignore')
            result = _setup_gitignore('/ws')
            self.assertEqual(result, 'using existing .gitignore')

    def test_copies_p4ignore(self):
        def exists_side_effect(path):
            return path.endswith('.p4ignore')

        with mock.patch('os.path.exists', side_effect=exists_side_effect), \
                mock.patch('shutil.copy2') as mock_copy:
            result = _setup_gitignore('/ws')
            self.assertEqual(result, 'copied .p4ignore to .gitignore')
            mock_copy.assert_called_once_with(
                os.path.join('/ws', '.p4ignore'),
                os.path.join('/ws', '.gitignore'))

    def test_creates_empty_gitignore(self):
        with mock.patch('os.path.exists', return_value=False), \
                mock.patch('builtins.open', mock.mock_open()) as mock_file:
            result = _setup_gitignore('/ws')
            self.assertEqual(result, 'created empty .gitignore')
            mock_file.assert_called_once_with(
                os.path.join('/ws', '.gitignore'), 'w')


class TestInitCommand(unittest.TestCase):
    def _make_args(self):
        return mock.Mock(spec=['command', 'verbose'])

    @mock.patch('git_p4son.init._setup_gitignore', return_value='created empty .gitignore')
    @mock.patch('git_p4son.init.run_with_output')
    @mock.patch('git_p4son.init._check_clobber', return_value=True)
    @mock.patch('git_p4son.init._check_p4_workspace', return_value=True)
    @mock.patch('os.path.exists', return_value=False)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_success(self, mock_cwd, mock_exists, mock_p4, mock_clobber,
                     mock_run, mock_gitignore):
        result = init_command(self._make_args())
        self.assertEqual(result, 0)
        # Should have called git init, git add, git commit
        self.assertEqual(mock_run.call_count, 3)

    @mock.patch('git_p4son.init._check_p4_workspace', return_value=False)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_not_p4_workspace(self, mock_cwd, mock_p4):
        result = init_command(self._make_args())
        self.assertEqual(result, 1)

    @mock.patch('git_p4son.init._check_clobber', return_value=False)
    @mock.patch('git_p4son.init._check_p4_workspace', return_value=True)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_no_clobber(self, mock_cwd, mock_p4, mock_clobber):
        result = init_command(self._make_args())
        self.assertEqual(result, 1)

    @mock.patch('git_p4son.init._setup_gitignore', return_value='using existing .gitignore')
    @mock.patch('git_p4son.init._check_clobber', return_value=True)
    @mock.patch('git_p4son.init._check_p4_workspace', return_value=True)
    @mock.patch('os.path.exists', return_value=True)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_existing_repo_skips_git_init(self, mock_cwd, mock_exists,
                                          mock_p4, mock_clobber,
                                          mock_gitignore):
        result = init_command(self._make_args())
        self.assertEqual(result, 0)
        # No git commands should be called (no init, no add, no commit)
        # run_with_output is not even imported/mocked here, so if it were
        # called it would actually run. Instead, mock it to verify.

    @mock.patch('git_p4son.init.run_with_output')
    @mock.patch('git_p4son.init._setup_gitignore', return_value='using existing .gitignore')
    @mock.patch('git_p4son.init._check_clobber', return_value=True)
    @mock.patch('git_p4son.init._check_p4_workspace', return_value=True)
    @mock.patch('os.path.exists', return_value=True)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_existing_repo_no_git_commands(self, mock_cwd, mock_exists,
                                           mock_p4, mock_clobber,
                                           mock_gitignore, mock_run):
        result = init_command(self._make_args())
        self.assertEqual(result, 0)
        mock_run.assert_not_called()
