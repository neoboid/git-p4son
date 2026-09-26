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

    def test_empty_list_prints_a_hint(self):
        self.assertEqual(self._run('list'), 0)
        printed = self._printed()
        self.assertEqual(len(printed), 1)
        self.assertIn('sync-split-users add', printed[0])
        self.mock_user.assert_not_called()


if __name__ == '__main__':
    unittest.main()
