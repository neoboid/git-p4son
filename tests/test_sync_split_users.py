"""Tests for git_p4son.sync_split_users module."""

import tempfile
import unittest
from unittest import mock

from git_p4son.common import RunError
from git_p4son.config import load_config, save_config
from git_p4son.sync_split_users import (
    USER_PLACEHOLDER,
    get_split_users,
    set_split_users,
    sync_split_users_command,
)


class TestSplitUsersConfig(unittest.TestCase):
    def test_empty_when_not_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(get_split_users(tmpdir), [])

    def test_empty_when_sync_section_lacks_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            save_config(tmpdir, {'sync': {'other': 'value'}})
            self.assertEqual(get_split_users(tmpdir), [])

    def test_set_and_read_back_in_order(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            users = ['bob', USER_PLACEHOLDER, 'alice']
            set_split_users(tmpdir, users)
            self.assertEqual(get_split_users(tmpdir), users)

    def test_set_empty_list(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            set_split_users(tmpdir, ['alice'])
            set_split_users(tmpdir, [])
            self.assertEqual(get_split_users(tmpdir), [])
            self.assertEqual(load_config(tmpdir)['sync']['split-users'], [])

    def test_set_preserves_other_settings(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            save_config(tmpdir, {'depot': {'root': '//ws'},
                                 'sync': {'other': 'value'}})
            set_split_users(tmpdir, ['alice'])
            config = load_config(tmpdir)
            self.assertEqual(config['depot'], {'root': '//ws'})
            self.assertEqual(config['sync']['other'], 'value')

    def test_non_list_value_is_ignored(self):
        """A hand-edited single string is not split into characters."""
        with tempfile.TemporaryDirectory() as tmpdir:
            save_config(tmpdir, {'sync': {'split-users': 'alice'}})
            self.assertEqual(get_split_users(tmpdir), [])

    def test_non_string_entries_are_ignored(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            save_config(tmpdir, {'sync': {'split-users': ['alice', 1]}})
            self.assertEqual(get_split_users(tmpdir), ['alice'])


class TestSyncSplitUsersList(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.ws = tempdir.name
        patcher = mock.patch('git_p4son.sync_split_users.log')
        self.mock_log = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch('git_p4son.sync_split_users.get_p4_user',
                             return_value='alice')
        self.mock_user = patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, action):
        args = mock.Mock(workspace_dir=self.ws, split_users_action=action)
        return sync_split_users_command(args)

    def _printed(self):
        return [c.args[0] for c in self.mock_log.info.call_args_list]

    def test_lists_users_in_order(self):
        set_split_users(self.ws, ['bob', 'carol'])
        self.assertEqual(self._run('list'), 0)
        self.assertEqual(self._printed(), ['bob', 'carol'])
        self.mock_user.assert_not_called()

    def test_bare_command_lists(self):
        set_split_users(self.ws, ['bob'])
        self.assertEqual(self._run(None), 0)
        self.assertEqual(self._printed(), ['bob'])

    def test_placeholder_shown_with_resolved_name(self):
        set_split_users(self.ws, [USER_PLACEHOLDER, 'bob'])
        self.assertEqual(self._run('list'), 0)
        self.assertEqual(self._printed(), ['$(user) (alice)', 'bob'])

    def test_placeholder_shown_bare_when_p4_fails(self):
        self.mock_user.side_effect = RunError('p4 info failed')
        set_split_users(self.ws, [USER_PLACEHOLDER, 'bob'])
        self.assertEqual(self._run('list'), 0)
        self.assertEqual(self._printed(), ['$(user)', 'bob'])

    def test_placeholder_shown_bare_without_p4_installed(self):
        self.mock_user.side_effect = FileNotFoundError('p4')
        set_split_users(self.ws, [USER_PLACEHOLDER])
        self.assertEqual(self._run('list'), 0)
        self.assertEqual(self._printed(), ['$(user)'])

    def test_empty_list_prints_a_hint(self):
        self.assertEqual(self._run('list'), 0)
        printed = self._printed()
        self.assertEqual(len(printed), 1)
        self.assertIn('sync-split-users add --me', printed[0])
        self.mock_user.assert_not_called()


class TestSyncSplitUsersAdd(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.ws = tempdir.name
        patcher = mock.patch('git_p4son.sync_split_users.log')
        self.mock_log = patcher.start()
        self.addCleanup(patcher.stop)
        # The server knows alice (spelled "Alice") and bob.
        patcher = mock.patch(
            'git_p4son.sync_split_users.get_existing_p4_users',
            side_effect=lambda names, _ws: [
                {'alice': 'Alice', 'bob': 'bob'}[n.lower()] for n in names
                if n.lower() in ('alice', 'bob')])
        self.mock_existing = patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, *names, me=False):
        args = mock.Mock(workspace_dir=self.ws, split_users_action='add',
                         names=list(names), me=me)
        return sync_split_users_command(args)

    def test_adds_users_in_order(self):
        self.assertEqual(self._run('bob', 'Alice'), 0)
        self.assertEqual(get_split_users(self.ws), ['bob', 'Alice'])
        self.mock_existing.assert_called_once_with(['bob', 'Alice'], self.ws)

    def test_appends_to_existing_users(self):
        set_split_users(self.ws, ['bob'])
        self.assertEqual(self._run('Alice'), 0)
        self.assertEqual(get_split_users(self.ws), ['bob', 'Alice'])

    def test_stores_the_server_spelling(self):
        self.assertEqual(self._run('ALICE'), 0)
        self.assertEqual(get_split_users(self.ws), ['Alice'])

    def test_already_present_is_not_an_error(self):
        set_split_users(self.ws, ['Alice'])
        self.assertEqual(self._run('alice', 'bob'), 0)
        self.assertEqual(get_split_users(self.ws), ['Alice', 'bob'])
        self.mock_log.info.assert_any_call('Alice is already a split user')

    def test_repeated_names_are_added_once(self):
        self.assertEqual(self._run('bob', 'BOB', 'bob'), 0)
        self.assertEqual(get_split_users(self.ws), ['bob'])

    def test_unknown_user_adds_nothing(self):
        set_split_users(self.ws, ['bob'])
        self.assertEqual(self._run('Alice', 'nobody'), 1)
        self.assertEqual(get_split_users(self.ws), ['bob'])
        self.mock_log.error.assert_called_once_with(
            'No such Perforce user: nobody')

    def test_me_adds_the_placeholder_without_checking(self):
        self.assertEqual(self._run(me=True), 0)
        self.assertEqual(get_split_users(self.ws), [USER_PLACEHOLDER])
        self.mock_existing.assert_not_called()

    def test_quoted_placeholder_is_the_same_as_me(self):
        self.assertEqual(self._run(USER_PLACEHOLDER), 0)
        self.assertEqual(self._run(me=True), 0)
        self.assertEqual(get_split_users(self.ws), [USER_PLACEHOLDER])
        self.mock_existing.assert_not_called()

    def test_me_combined_with_names(self):
        self.assertEqual(self._run('bob', me=True), 0)
        self.assertEqual(get_split_users(self.ws), [USER_PLACEHOLDER, 'bob'])
        self.mock_existing.assert_called_once_with(['bob'], self.ws)

    def test_nothing_given_is_an_error(self):
        self.assertEqual(self._run(), 1)
        self.mock_log.error.assert_called_once()
        self.assertEqual(get_split_users(self.ws), [])

    def test_nothing_written_when_all_already_present(self):
        set_split_users(self.ws, ['bob'])
        with mock.patch(
                'git_p4son.sync_split_users.set_split_users') as mock_set:
            self.assertEqual(self._run('bob'), 0)
        mock_set.assert_not_called()


class TestSyncSplitUsersDelete(unittest.TestCase):
    def setUp(self):
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.ws = tempdir.name
        patcher = mock.patch('git_p4son.sync_split_users.log')
        self.mock_log = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch('git_p4son.sync_split_users.get_p4_user',
                             return_value='Carol')
        self.mock_user = patcher.start()
        self.addCleanup(patcher.stop)
        set_split_users(self.ws, [USER_PLACEHOLDER, 'Alice', 'bob'])

    def _run(self, *names, me=False):
        args = mock.Mock(workspace_dir=self.ws, split_users_action='delete',
                         names=list(names), me=me)
        return sync_split_users_command(args)

    def test_removes_users_keeping_the_rest_in_order(self):
        self.assertEqual(self._run('Alice'), 0)
        self.assertEqual(get_split_users(self.ws), [USER_PLACEHOLDER, 'bob'])

    def test_match_is_case_insensitive(self):
        self.assertEqual(self._run('ALICE', 'Bob'), 0)
        self.assertEqual(get_split_users(self.ws), [USER_PLACEHOLDER])
        self.mock_log.success.assert_any_call('Alice')

    def test_me_removes_the_placeholder(self):
        self.assertEqual(self._run(me=True), 0)
        self.assertEqual(get_split_users(self.ws), ['Alice', 'bob'])
        self.mock_user.assert_not_called()

    def test_quoted_placeholder_is_the_same_as_me(self):
        self.assertEqual(self._run(USER_PLACEHOLDER), 0)
        self.assertEqual(get_split_users(self.ws), ['Alice', 'bob'])

    def test_removing_the_last_user_leaves_an_empty_list(self):
        self.assertEqual(self._run('Alice', 'bob', me=True), 0)
        self.assertEqual(get_split_users(self.ws), [])

    def test_unknown_name_removes_nothing(self):
        self.assertEqual(self._run('bob', 'dave'), 1)
        self.assertEqual(get_split_users(self.ws),
                         [USER_PLACEHOLDER, 'Alice', 'bob'])
        self.mock_log.error.assert_called_once_with('dave is not a split user')

    def test_hints_at_me_when_naming_yourself(self):
        """The list holds $(user), not the name it resolves to."""
        self.assertEqual(self._run('carol'), 1)
        self.mock_log.error.assert_called_once_with(
            'carol is not a split user')
        self.mock_log.info.assert_called_once_with(
            '$(user) stands for Carol, remove it with --me')

    def test_no_hint_without_the_placeholder(self):
        set_split_users(self.ws, ['Alice'])
        self.assertEqual(self._run('carol'), 1)
        self.mock_log.info.assert_not_called()
        self.mock_user.assert_not_called()

    def test_no_hint_when_p4_fails(self):
        self.mock_user.side_effect = RunError('p4 info failed')
        self.assertEqual(self._run('carol'), 1)
        self.mock_log.info.assert_not_called()

    def test_nothing_given_is_an_error(self):
        self.assertEqual(self._run(), 1)
        self.mock_log.error.assert_called_once()
        self.assertEqual(get_split_users(self.ws),
                         [USER_PLACEHOLDER, 'Alice', 'bob'])


if __name__ == '__main__':
    unittest.main()
