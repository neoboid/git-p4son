"""Tests for git_p4son.list_changes module."""

import unittest
from unittest import mock

from git_p4son.list_changes import list_changes_command


class TestListChangesCommand(unittest.TestCase):
    @mock.patch('git_p4son.list_changes.log')
    @mock.patch('git_p4son.list_changes.get_commit_subjects_since')
    def test_prints_enumerated_subjects(self, mock_subjects, mock_log):
        mock_subjects.return_value = ['Add feature', 'Fix bug', 'Update docs']
        args = mock.Mock(base_branch='main', workspace_dir='/ws')
        self.assertEqual(list_changes_command(args), 0)
        mock_subjects.assert_called_once_with('main', '/ws')
        mock_log.info.assert_called_once_with(
            '1. Add feature\n2. Fix bug\n3. Update docs')

    @mock.patch('git_p4son.list_changes.log')
    @mock.patch('git_p4son.list_changes.get_commit_subjects_since')
    def test_no_changes(self, mock_subjects, mock_log):
        mock_subjects.return_value = []
        args = mock.Mock(base_branch='HEAD~1', workspace_dir='/ws')
        self.assertEqual(list_changes_command(args), 0)
        mock_log.info.assert_not_called()
        mock_log.success.assert_called_once_with('No changes found')


if __name__ == '__main__':
    unittest.main()
