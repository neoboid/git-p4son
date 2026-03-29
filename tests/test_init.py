"""Tests for git_p4son.init module."""

import os
import unittest
from unittest import mock

from git_p4son.common import CommandError
from git_p4son.init import (
    _compute_cwd_depot_root,
    _configure_depot_root,
    _setup_gitignore,
    _validate_depot_root,
    init_command,
)
from git_p4son.perforce import P4ClientSpec
from tests.helpers import make_run_result


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


_MOCK_SPEC = P4ClientSpec(
    name='my-client', root='/ws',
    options=['noallwrite', 'clobber', 'nocompress'], stream=None)


class TestInitCommand(unittest.TestCase):
    def _make_args(self):
        return mock.Mock(spec=['command', 'verbose'])

    @mock.patch('git_p4son.init.resolve_editor', return_value='vim')
    @mock.patch('git_p4son.init._setup_gitignore', return_value='created empty .gitignore')
    @mock.patch('git_p4son.init.run_with_output')
    @mock.patch('git_p4son.init._configure_depot_root', return_value=True)
    @mock.patch('git_p4son.init.get_client_spec', return_value=_MOCK_SPEC)
    @mock.patch('os.path.exists', return_value=False)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_success(self, mock_cwd, mock_exists, mock_spec, mock_depot,
                     mock_run, mock_gitignore, mock_editor):
        result = init_command(self._make_args())
        self.assertEqual(result, 0)
        # Should have called git init, git add, git commit
        self.assertEqual(mock_run.call_count, 3)

    @mock.patch('git_p4son.init.get_client_spec', return_value=None)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_not_p4_workspace(self, mock_cwd, mock_spec):
        result = init_command(self._make_args())
        self.assertEqual(result, 1)

    @mock.patch('git_p4son.init.resolve_editor', return_value='vim')
    @mock.patch('git_p4son.init._setup_gitignore', return_value='.gitignore already exist')
    @mock.patch('git_p4son.init._configure_depot_root', return_value=True)
    @mock.patch('git_p4son.init.get_client_spec', return_value=_MOCK_SPEC)
    @mock.patch('os.path.exists', return_value=True)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_existing_repo_skips_git_init(self, mock_cwd, mock_exists,
                                          mock_spec, mock_depot,
                                          mock_gitignore, mock_editor):
        result = init_command(self._make_args())
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.init._configure_depot_root', return_value=False)
    @mock.patch('git_p4son.init.get_client_spec', return_value=_MOCK_SPEC)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_depot_root_abort(self, mock_cwd, mock_spec, mock_depot):
        result = init_command(self._make_args())
        self.assertEqual(result, 1)


if __name__ == '__main__':
    unittest.main()
