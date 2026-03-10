"""Tests for git_p4son.init module."""

import os
import unittest
from unittest import mock

from git_p4son.common import CommandError
from git_p4son.init import (
    _check_clobber,
    _compute_cwd_depot_root,
    _configure_depot_root,
    _get_p4_workspace_root,
    _setup_gitignore,
    _validate_depot_root,
    get_p4_client_name,
    init_command,
)
from tests.helpers import make_run_result


class TestGetP4ClientName(unittest.TestCase):
    @mock.patch('subprocess.run')
    def test_returns_client_name(self, mock_run):
        mock_run.return_value = mock.Mock(
            stdout='Client name: my-ws\nClient root: /ws\n')
        self.assertEqual(get_p4_client_name('/ws'), 'my-ws')

    @mock.patch('subprocess.run')
    def test_unknown_returns_none(self, mock_run):
        mock_run.return_value = mock.Mock(
            stdout='Client name: *unknown*\n')
        self.assertIsNone(get_p4_client_name('/ws'))

    @mock.patch('subprocess.run')
    def test_no_client_line_returns_none(self, mock_run):
        mock_run.return_value = mock.Mock(
            stdout='Server address: ssl:perforce:1666\n')
        self.assertIsNone(get_p4_client_name('/ws'))


class TestCheckClobber(unittest.TestCase):
    def test_clobber_enabled(self):
        lines = ['Options:\tallwrite clobber compress']
        self.assertTrue(_check_clobber(lines))

    def test_noclobber(self):
        lines = ['Options:\tallwrite noclobber compress']
        self.assertFalse(_check_clobber(lines))

    def test_no_options_line(self):
        lines = ['Root:\t/ws']
        self.assertFalse(_check_clobber(lines))

    def test_empty_lines(self):
        self.assertFalse(_check_clobber([]))


class TestGetP4WorkspaceRoot(unittest.TestCase):
    def test_extracts_root(self):
        lines = ['Client:\tmy-ws',
                 'Root:\t/home/user/workspace', 'Options:\tclobber']
        self.assertEqual(_get_p4_workspace_root(lines), '/home/user/workspace')

    def test_no_root_line(self):
        lines = ['Client:\tmy-ws', 'Options:\tclobber']
        self.assertIsNone(_get_p4_workspace_root(lines))

    def test_empty_lines(self):
        self.assertIsNone(_get_p4_workspace_root([]))


class TestComputeCwdDepotRoot(unittest.TestCase):
    def test_subdirectory(self):
        result = _compute_cwd_depot_root('my-ws', '/ws/Engine/Source', '/ws')
        self.assertEqual(result, '//my-ws/Engine/Source')

    def test_at_workspace_root(self):
        result = _compute_cwd_depot_root('my-ws', '/ws', '/ws')
        self.assertIsNone(result)

    def test_nested_subdirectory(self):
        result = _compute_cwd_depot_root('my-ws', '/ws/a/b/c', '/ws')
        self.assertEqual(result, '//my-ws/a/b/c')


class TestValidateDepotRoot(unittest.TestCase):
    @mock.patch('git_p4son.init.run')
    def test_valid_root(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 on 2024/01/01'])
        self.assertTrue(_validate_depot_root('//my-ws', '/ws'))
        mock_run.assert_called_once_with(
            ['p4', 'changes', '-m1', '-s', 'submitted', '//my-ws/...'],
            cwd='/ws')

    @mock.patch('git_p4son.init.run', side_effect=CommandError('p4 error'))
    def test_invalid_root(self, mock_run):
        self.assertFalse(_validate_depot_root('//bad-root', '/ws'))


class TestSetupGitignore(unittest.TestCase):
    def test_existing_gitignore_left_as_is(self):
        with mock.patch('os.path.exists') as mock_exists:
            mock_exists.side_effect = lambda p: p.endswith('.gitignore')
            result = _setup_gitignore('/ws')
            self.assertEqual(result, '.gitignore already exist')

    def test_copies_p4ignore(self):
        def exists_side_effect(path):
            return path.endswith('.p4ignore')

        with mock.patch('os.path.exists', side_effect=exists_side_effect), \
                mock.patch('shutil.copy2') as mock_copy:
            result = _setup_gitignore('/ws')
            self.assertEqual(result, 'copied .p4ignore to new .gitignore')
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


class TestConfigureDepotRoot(unittest.TestCase):
    @mock.patch('git_p4son.init._validate_depot_root', return_value=True)
    @mock.patch('git_p4son.init.get_depot_root', return_value='//depot')
    @mock.patch('git_p4son.init.save_config')
    def test_existing_valid_root_skips_save(self, mock_save, mock_get, mock_validate):
        result = _configure_depot_root('client', '/ws', '/ws')
        self.assertTrue(result)
        mock_save.assert_not_called()

    @mock.patch('git_p4son.init._select_depot_root', return_value='//depot/new')
    @mock.patch('git_p4son.init._validate_depot_root', return_value=False)
    @mock.patch('git_p4son.init.get_depot_root', return_value='//depot/old')
    @mock.patch('git_p4son.init.save_config')
    def test_existing_invalid_root_prompts_and_saves(self, mock_save, mock_get,
                                                     mock_validate, mock_select):
        result = _configure_depot_root('client', '/ws', '/ws')
        self.assertTrue(result)
        mock_save.assert_called_once_with(
            '/ws', {'depot': {'root': '//depot/new'}})

    @mock.patch('git_p4son.init._select_depot_root', return_value='//depot')
    @mock.patch('git_p4son.init.get_depot_root', return_value=None)
    @mock.patch('git_p4son.init.save_config')
    def test_no_existing_root_prompts_and_saves(self, mock_save, mock_get, mock_select):
        result = _configure_depot_root('client', '/ws', '/ws')
        self.assertTrue(result)
        mock_save.assert_called_once_with(
            '/ws', {'depot': {'root': '//depot'}})

    @mock.patch('git_p4son.init._select_depot_root', return_value=None)
    @mock.patch('git_p4son.init.get_depot_root', return_value=None)
    @mock.patch('git_p4son.init.save_config')
    def test_no_existing_root_user_aborts(self, mock_save, mock_get, mock_select):
        result = _configure_depot_root('client', '/ws', '/ws')
        self.assertFalse(result)
        mock_save.assert_not_called()


class TestInitCommand(unittest.TestCase):
    def _make_args(self):
        return mock.Mock(spec=['command', 'verbose'])

    @mock.patch('git_p4son.init._resolve_editor', return_value='vim')
    @mock.patch('git_p4son.init._setup_gitignore', return_value='created empty .gitignore')
    @mock.patch('git_p4son.init.run_with_output')
    @mock.patch('git_p4son.init._configure_depot_root', return_value='//my-client')
    @mock.patch('git_p4son.init._get_p4_workspace_root', return_value='/ws')
    @mock.patch('git_p4son.init._get_p4_client_spec', return_value=['Root:\t/ws'])
    @mock.patch('git_p4son.init._check_clobber', return_value=True)
    @mock.patch('git_p4son.init.get_p4_client_name', return_value='my-client')
    @mock.patch('os.path.exists', return_value=False)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_success(self, mock_cwd, mock_exists, mock_p4, mock_clobber,
                     mock_spec, mock_ws_root, mock_depot,
                     mock_run, mock_gitignore, mock_editor):
        result = init_command(self._make_args())
        self.assertEqual(result, 0)
        # Should have called git init, git add, git commit
        self.assertEqual(mock_run.call_count, 3)

    @mock.patch('git_p4son.init.get_p4_client_name', return_value=None)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_not_p4_workspace(self, mock_cwd, mock_p4):
        result = init_command(self._make_args())
        self.assertEqual(result, 1)

    @mock.patch('git_p4son.init._get_p4_client_spec', return_value=['Options:\tnoclobber'])
    @mock.patch('git_p4son.init._check_clobber', return_value=False)
    @mock.patch('git_p4son.init.get_p4_client_name', return_value='my-client')
    @mock.patch('os.getcwd', return_value='/ws')
    def test_no_clobber(self, mock_cwd, mock_p4, mock_clobber, mock_spec):
        result = init_command(self._make_args())
        self.assertEqual(result, 1)

    @mock.patch('git_p4son.init._resolve_editor', return_value='vim')
    @mock.patch('git_p4son.init._setup_gitignore', return_value='.gitignore already exist')
    @mock.patch('git_p4son.init.save_config')
    @mock.patch('git_p4son.init._configure_depot_root', return_value='//my-client')
    @mock.patch('git_p4son.init._get_p4_workspace_root', return_value='/ws')
    @mock.patch('git_p4son.init._get_p4_client_spec', return_value=['Root:\t/ws'])
    @mock.patch('git_p4son.init._check_clobber', return_value=True)
    @mock.patch('git_p4son.init.get_p4_client_name', return_value='my-client')
    @mock.patch('os.path.exists', return_value=True)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_existing_repo_skips_git_init(self, mock_cwd, mock_exists,
                                          mock_p4, mock_clobber, mock_spec,
                                          mock_ws_root, mock_depot,
                                          mock_save, mock_gitignore,
                                          mock_editor):
        result = init_command(self._make_args())
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.init._configure_depot_root', return_value=None)
    @mock.patch('git_p4son.init._get_p4_workspace_root', return_value='/ws')
    @mock.patch('git_p4son.init._get_p4_client_spec', return_value=['Root:\t/ws'])
    @mock.patch('git_p4son.init._check_clobber', return_value=True)
    @mock.patch('git_p4son.init.get_p4_client_name', return_value='my-client')
    @mock.patch('os.getcwd', return_value='/ws')
    def test_depot_root_abort(self, mock_cwd, mock_p4, mock_clobber,
                              mock_spec, mock_ws_root, mock_depot):
        result = init_command(self._make_args())
        self.assertEqual(result, 1)


if __name__ == '__main__':
    unittest.main()
