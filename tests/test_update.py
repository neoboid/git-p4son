"""Tests for git_p4son.update module."""

import unittest
from unittest import mock

from git_p4son.update import update_command


def _args(**overrides):
    defaults = dict(workspace_dir='/ws', changelist='100', dry_run=False,
                    base_branch='HEAD~1', no_desc=False, no_edit=False,
                    shelve=False)
    defaults.update(overrides)
    return mock.Mock(**defaults)


class TestUpdateCommandCleanWorkspace(unittest.TestCase):
    @mock.patch('git_p4son.update.open_changes_for_edit')
    @mock.patch('git_p4son.update.update_changelist')
    @mock.patch('git_p4son.lib.get_dirty_files',
                return_value=[('mod.txt', 'modify')])
    def test_refuses_dirty_workspace_before_touching_changelist(
            self, _dirty, mock_update, mock_open):
        rc = update_command(_args())
        self.assertEqual(rc, 1)
        mock_update.assert_not_called()
        mock_open.assert_not_called()

    @mock.patch('git_p4son.update.update_changelist')
    @mock.patch('git_p4son.lib.get_dirty_files',
                return_value=[('mod.txt', 'modify')])
    def test_refuses_dirty_workspace_on_dry_run(self, _dirty, mock_update):
        rc = update_command(_args(dry_run=True))
        self.assertEqual(rc, 1)
        mock_update.assert_not_called()

    @mock.patch('git_p4son.update.revert_stale_files', return_value=0)
    @mock.patch('git_p4son.update.open_changes_for_edit')
    @mock.patch('git_p4son.update.update_changelist')
    @mock.patch('git_p4son.lib.get_dirty_files', return_value=[])
    def test_clean_workspace_proceeds(self, _dirty, mock_update, mock_open,
                                      _revert):
        rc = update_command(_args())
        self.assertEqual(rc, 0)
        mock_update.assert_called_once()
        mock_open.assert_called_once()

    @mock.patch('git_p4son.update.update_changelist')
    @mock.patch('git_p4son.lib.get_dirty_files')
    def test_no_edit_skips_check(self, mock_dirty, mock_update):
        rc = update_command(_args(no_edit=True))
        self.assertEqual(rc, 0)
        mock_dirty.assert_not_called()
        mock_update.assert_called_once()


class TestUpdateCommandRevertStep(unittest.TestCase):
    def _run(self, **overrides):
        order = mock.Mock()
        with mock.patch('git_p4son.lib.get_dirty_files', return_value=[]), \
                mock.patch('git_p4son.update.update_changelist'), \
                mock.patch('git_p4son.update.open_changes_for_edit',
                           order.open), \
                mock.patch('git_p4son.update.revert_stale_files',
                           order.revert), \
                mock.patch('git_p4son.update.p4_shelve_changelist',
                           order.shelve):
            order.revert.return_value = 0
            rc = update_command(_args(**overrides))
        self.assertEqual(rc, 0)
        return order

    def test_reverts_after_opening_and_before_shelving(self):
        order = self._run(shelve=True)
        self.assertEqual([c[0] for c in order.mock_calls],
                         ['open', 'revert', 'shelve'])
        order.revert.assert_called_once_with('100', '/ws', False)

    def test_no_edit_skips_revert(self):
        order = self._run(no_edit=True, shelve=True)
        self.assertEqual([c[0] for c in order.mock_calls], ['shelve'])


if __name__ == '__main__':
    unittest.main()
