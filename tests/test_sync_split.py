"""Tests for git_p4son.sync_split module."""

import unittest
from unittest import mock

from git_p4son.common import RunError
from git_p4son.sync_split import sync_split_command


class TestSyncSplitCommand(unittest.TestCase):
    """sync-split only explains how to do the same with sync, and fails
    without syncing."""

    def setUp(self):
        patcher = mock.patch('git_p4son.sync_split.log')
        self.mock_log = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch('git_p4son.sync_split_users.get_p4_user',
                             return_value='me')
        self.mock_user = patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, changelist=None, user=None, dry_run=False):
        args = mock.Mock(changelist=changelist, user=user, dry_run=dry_run,
                         workspace_dir='/ws')
        with mock.patch('git_p4son.sync.sync_command') as mock_sync:
            rc = sync_split_command(args)
        mock_sync.assert_not_called()
        return rc

    def _printed(self):
        return [c.args[0] for c in self.mock_log.info.call_args_list]

    def test_defaults_to_the_current_user(self):
        self.assertEqual(self._run(), 1)
        self.mock_log.error.assert_called_once_with(
            'sync-split has been folded into sync.')
        self.assertEqual(self._printed(), [
            'Run instead:',
            '  git p4son sync -u me',
            'To split them out on every sync:',
            '  git p4son sync-split-users add --me',
        ])

    def test_falls_back_on_the_quoted_placeholder(self):
        self.mock_user.side_effect = RunError('p4 info failed')
        self.assertEqual(self._run(), 1)
        self.assertIn("  git p4son sync -u '$(user)'", self._printed())

    def test_users_changelist_and_dry_run_carry_over(self):
        self.assertEqual(
            self._run('12345', user=['alice', 'bob'], dry_run=True), 1)
        self.assertEqual(self._printed(), [
            'Run instead:',
            '  git p4son sync -u alice -u bob --dry-run 12345',
            'To split them out on every sync:',
            '  git p4son sync-split-users add alice bob',
        ])
        self.mock_user.assert_not_called()


if __name__ == '__main__':
    unittest.main()
