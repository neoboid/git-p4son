"""Tests for git_p4son.init module."""

import os
import tempfile
import unittest
from unittest import mock

from git_p4son.common import CommandError
from git_p4son.config import load_config
from git_p4son.init import (
    _ask_yes_no,
    _compute_cwd_depot_root,
    _configure_depot_root,
    _configure_split_users,
    _configure_writable_mode,
    _setup_gitignore,
    _validate_depot_root,
    init_command,
)
from git_p4son.perforce import P4ClientSpec
from git_p4son.sync_split_users import get_split_users, set_split_users
from git_p4son.writable import is_writable_mode, set_writable_mode
from tests.helpers import make_run_result


class TestComputeCwdDepotRoot(unittest.TestCase):
    def test_subdirectory(self):
        result = _compute_cwd_depot_root('/ws/Engine/Source', '/ws')
        self.assertEqual(result, '//$(workspace)/Engine/Source')

    def test_at_workspace_root(self):
        result = _compute_cwd_depot_root('/ws', '/ws')
        self.assertIsNone(result)

    def test_nested_subdirectory(self):
        result = _compute_cwd_depot_root('/ws/a/b/c', '/ws')
        self.assertEqual(result, '//$(workspace)/a/b/c')


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


class TestSelectDepotRoot(unittest.TestCase):
    @mock.patch('builtins.input', side_effect=EOFError)
    def test_eof_aborts_cleanly(self, _input):
        from git_p4son.init import _select_depot_root
        result = _select_depot_root('client', '/ws/sub', '/ws')
        self.assertIsNone(result)

    @mock.patch('git_p4son.init._validate_depot_root', return_value=True)
    @mock.patch('builtins.input', return_value='1')
    def test_entire_workspace_stores_template(self, _input, mock_validate):
        from git_p4son.init import _select_depot_root
        result = _select_depot_root('my-ws', '/ws/sub', '/ws')
        # The stored root keeps the placeholder; validation uses the resolved
        # path so p4 sees a real depot.
        self.assertEqual(result, '//$(workspace)')
        mock_validate.assert_called_once_with('//my-ws', '/ws/sub')

    @mock.patch('git_p4son.init._validate_depot_root', return_value=True)
    @mock.patch('builtins.input', return_value='2')
    def test_current_directory_stores_template(self, _input, mock_validate):
        from git_p4son.init import _select_depot_root
        result = _select_depot_root('my-ws', '/ws/Engine/Source', '/ws')
        self.assertEqual(result, '//$(workspace)/Engine/Source')
        mock_validate.assert_called_once_with(
            '//my-ws/Engine/Source', '/ws/Engine/Source')


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

    @mock.patch('git_p4son.init._validate_depot_root', return_value=True)
    @mock.patch('git_p4son.init.get_depot_root',
                return_value='//$(workspace)/Engine')
    @mock.patch('git_p4son.init.save_config')
    def test_existing_template_root_validated_resolved(self, mock_save,
                                                       mock_get, mock_validate):
        """An existing $(workspace) root is validated against the resolved
        path, not the literal placeholder."""
        result = _configure_depot_root('my-ws', '/ws', '/ws')
        self.assertTrue(result)
        mock_validate.assert_called_once_with('//my-ws/Engine', '/ws')
        mock_save.assert_not_called()


_MOCK_SPEC = P4ClientSpec(
    name='my-client', root='/ws',
    options=['noallwrite', 'clobber', 'nocompress'], stream=None,
    line_end='local')


class TestAskYesNo(unittest.TestCase):
    @mock.patch('builtins.input', return_value='')
    def test_empty_answer_keeps_current_value(self, _input):
        self.assertTrue(_ask_yes_no('Q?', True))
        self.assertFalse(_ask_yes_no('Q?', False))

    @mock.patch('builtins.input', side_effect=['maybe', 'Y'])
    def test_reprompts_until_valid(self, mock_input):
        self.assertTrue(_ask_yes_no('Q?', False))
        self.assertEqual(mock_input.call_count, 2)

    @mock.patch('builtins.input', return_value='no')
    def test_accepts_words(self, _input):
        self.assertFalse(_ask_yes_no('Q?', True))

    @mock.patch('builtins.input', return_value='')
    def test_question_and_default_are_shown_in_the_prompt(self, mock_input):
        _ask_yes_no('Keep it?', True)
        self.assertEqual(mock_input.call_args.args[0], 'Keep it? [Y/n]: ')
        _ask_yes_no('Keep it?', False)
        self.assertEqual(mock_input.call_args.args[0], 'Keep it? [y/N]: ')

    @mock.patch('builtins.input', side_effect=EOFError)
    def test_eof_returns_none(self, _input):
        self.assertIsNone(_ask_yes_no('Q?', False))


class TestConfigureWritableMode(unittest.TestCase):
    @mock.patch('git_p4son.init._ask_yes_no', return_value=True)
    def test_saves_answer(self, _ask):
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(_configure_writable_mode(ws), (True, True))
            self.assertTrue(is_writable_mode(ws))

    @mock.patch('git_p4son.init._ask_yes_no', return_value=True)
    def test_unchanged_answer_is_not_a_change(self, _ask):
        with tempfile.TemporaryDirectory() as ws:
            set_writable_mode(ws, True)
            self.assertEqual(_configure_writable_mode(ws), (True, False))

    @mock.patch('git_p4son.init._ask_yes_no', return_value=None)
    def test_eof_leaves_config_untouched(self, _ask):
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(_configure_writable_mode(ws), (False, False))
            self.assertEqual(load_config(ws), {})


class TestConfigureSplitUsers(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.ws = tempdir.name
        patcher = mock.patch('git_p4son.sync_split_users.get_p4_user',
                             return_value='alice')
        self.mock_user = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch('git_p4son.init.log')
        self.mock_log = patcher.start()
        self.addCleanup(patcher.stop)

    def _answer(self, answer):
        with mock.patch('git_p4son.init._ask_yes_no',
                        return_value=answer) as mock_ask, \
                mock.patch('builtins.print'):
            _configure_split_users(self.ws)
        return mock_ask

    def test_question_names_the_user_and_defaults_to_off(self):
        mock_ask = self._answer(None)
        mock_ask.assert_called_once_with(
            'Sync your own changelists (alice) individually?', False)

    def test_default_is_on_when_already_configured(self):
        set_split_users(self.ws, ['$(user)'])
        mock_ask = self._answer(None)
        self.assertTrue(mock_ask.call_args.args[1])

    def test_yes_adds_the_placeholder_after_other_users(self):
        set_split_users(self.ws, ['bob'])
        self._answer(True)
        self.assertEqual(get_split_users(self.ws), ['bob', '$(user)'])

    def test_no_removes_only_the_placeholder(self):
        set_split_users(self.ws, ['bob', '$(user)', 'carol'])
        self._answer(False)
        self.assertEqual(get_split_users(self.ws), ['bob', 'carol'])

    def test_unchanged_answer_writes_nothing(self):
        self._answer(False)
        self.assertEqual(load_config(self.ws), {})
        self.mock_log.success.assert_called_once_with('off (unchanged)')

    def test_eof_writes_nothing(self):
        self._answer(None)
        self.assertEqual(load_config(self.ws), {})

    def test_skipped_without_a_current_user(self):
        self.mock_user.return_value = None
        mock_ask = self._answer(True)
        mock_ask.assert_not_called()
        self.mock_log.warning.assert_called_once()
        self.assertEqual(load_config(self.ws), {})


class TestInitCommand(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch('git_p4son.init._configure_writable_mode',
                             return_value=(False, False))
        self.mock_writable = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch('git_p4son.init._configure_split_users')
        self.mock_split_users = patcher.start()
        self.addCleanup(patcher.stop)

    def _make_args(self):
        return mock.Mock(spec=['command', 'verbose'])

    def _next_steps(self, has_commits):
        with mock.patch('git_p4son.init.resolve_editor', return_value='vim'), \
                mock.patch('git_p4son.init._setup_gitignore',
                           return_value='created'), \
                mock.patch('git_p4son.init._has_commits',
                           return_value=has_commits), \
                mock.patch('git_p4son.init.run_with_output'), \
                mock.patch('git_p4son.init._configure_depot_root',
                           return_value=True), \
                mock.patch('git_p4son.init.get_client_spec',
                           return_value=_MOCK_SPEC), \
                mock.patch('os.path.exists', return_value=True), \
                mock.patch('os.getcwd', return_value='/ws'), \
                mock.patch('git_p4son.init.log') as mock_log:
            self.assertEqual(init_command(self._make_args()), 0)
        return [str(c.args[0]) for c in mock_log.info.call_args_list]

    def test_configures_split_users(self):
        self._next_steps(has_commits=False)
        self.mock_split_users.assert_called_once_with('/ws')

    def test_fresh_repo_with_writable_mode_suggests_apply(self):
        self.mock_writable.return_value = (True, True)
        self.assertIn('* git p4son writable apply',
                      self._next_steps(has_commits=False))

    def test_fresh_repo_without_writable_mode_does_not(self):
        steps = self._next_steps(has_commits=False)
        self.assertFalse(any('writable' in step for step in steps))

    def test_existing_repo_suggests_apply_when_mode_changes(self):
        self.mock_writable.return_value = (False, True)
        steps = self._next_steps(has_commits=True)
        self.assertTrue(any('writable apply' in step for step in steps))

    def test_existing_repo_unchanged_mode_suggests_nothing(self):
        self.mock_writable.return_value = (True, False)
        steps = self._next_steps(has_commits=True)
        self.assertFalse(any('writable' in step for step in steps))

    @mock.patch('git_p4son.init.resolve_editor', return_value='vim')
    @mock.patch('git_p4son.init._setup_gitignore', return_value='created empty .gitignore')
    @mock.patch('git_p4son.init._has_commits', return_value=False)
    @mock.patch('git_p4son.init.run_with_output')
    @mock.patch('git_p4son.init._configure_depot_root', return_value=True)
    @mock.patch('git_p4son.init.get_client_spec', return_value=_MOCK_SPEC)
    @mock.patch('os.path.exists', return_value=False)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_success(self, mock_cwd, mock_exists, mock_spec, mock_depot,
                     mock_run, mock_has_commits, mock_gitignore,
                     mock_editor):
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
    @mock.patch('git_p4son.init._has_commits', return_value=True)
    @mock.patch('git_p4son.init.run_with_output')
    @mock.patch('git_p4son.init._configure_depot_root', return_value=True)
    @mock.patch('git_p4son.init.get_client_spec', return_value=_MOCK_SPEC)
    @mock.patch('os.path.exists', return_value=True)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_existing_repo_skips_git_init(self, mock_cwd, mock_exists,
                                          mock_spec, mock_depot, mock_run,
                                          mock_has_commits, mock_gitignore,
                                          mock_editor):
        result = init_command(self._make_args())
        self.assertEqual(result, 0)
        mock_run.assert_not_called()

    @mock.patch('git_p4son.init.resolve_editor', return_value='vim')
    @mock.patch('git_p4son.init._setup_gitignore', return_value='.gitignore already exist')
    @mock.patch('git_p4son.init._has_commits', return_value=False)
    @mock.patch('git_p4son.init.run_with_output')
    @mock.patch('git_p4son.init._configure_depot_root', return_value=True)
    @mock.patch('git_p4son.init.get_client_spec', return_value=_MOCK_SPEC)
    @mock.patch('os.path.exists', return_value=True)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_existing_repo_without_commits_creates_initial_commit(
            self, mock_cwd, mock_exists, mock_spec, mock_depot, mock_run,
            mock_has_commits, mock_gitignore, mock_editor):
        """A previous init may have failed at the commit step (e.g.
        user.email not configured); re-running init must recover instead
        of treating the unborn repo as fully initialized."""
        result = init_command(self._make_args())
        self.assertEqual(result, 0)
        # git add + git commit, but no git init
        self.assertEqual(mock_run.call_count, 2)
        commands = [call.args[0] for call in mock_run.call_args_list]
        self.assertNotIn(['git', 'init'], commands)

    @mock.patch('git_p4son.init._configure_depot_root', return_value=False)
    @mock.patch('git_p4son.init.get_client_spec', return_value=_MOCK_SPEC)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_depot_root_abort(self, mock_cwd, mock_spec, mock_depot):
        result = init_command(self._make_args())
        self.assertEqual(result, 1)


if __name__ == '__main__':
    unittest.main()
