"""Tests for git_p4son.list_changes module."""

import unittest
from unittest import mock

from git_p4son.git_backend import GitBackend
from git_p4son.list_changes import list_changes_command
from tests.helpers import captured_output, GitRepoTestCase


class TestListChangesCommand(unittest.TestCase):
    @mock.patch('git_p4son.list_changes.get_commit_subjects_since')
    def test_prints_enumerated_subjects(self, mock_subjects):
        mock_subjects.return_value = ['Add feature', 'Fix bug', 'Update docs']
        args = mock.Mock(base_branch='main', workspace_dir='/ws',
                         backend=GitBackend('/ws'))
        with captured_output() as out:
            self.assertEqual(list_changes_command(args), 0)
        mock_subjects.assert_called_once_with('main', '/ws', 'HEAD')
        self.assertEqual(out.getvalue().lstrip('\n'),
                         '# Listing changes\n1. Add feature\n2. Fix bug\n3. Update docs\n')

    @mock.patch('git_p4son.list_changes.get_commit_subjects_since')
    def test_no_changes(self, mock_subjects):
        mock_subjects.return_value = []
        args = mock.Mock(base_branch='HEAD~1', workspace_dir='/ws',
                         backend=GitBackend('/ws'))
        with captured_output() as out:
            self.assertEqual(list_changes_command(args), 0)
        self.assertEqual(out.getvalue().lstrip('\n'),
                         '# Listing changes\n[ok] No changes found\n')


class TestListChangesPorcelain(GitRepoTestCase):
    def build_workspace(self):
        super().build_workspace()
        for subject in ('Initial', 'Add feature', 'Fix bug'):
            self.commit(subject)

    def test_prints_only_the_list(self):
        self.assertEqual(self.p4son('list-changes', '-b', 'HEAD~2', '--porcelain'),
                         '1. Add feature\n2. Fix bug\n')

    def test_prints_nothing_without_changes(self):
        self.assertEqual(self.p4son(
            'list-changes', '-b', 'HEAD', '--porcelain'), '')


if __name__ == '__main__':
    unittest.main()
