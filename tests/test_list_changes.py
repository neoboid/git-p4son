"""Tests for git_p4son.list_changes module."""

import unittest
from unittest import mock

from git_p4son.common import CommandError, RunError
from git_p4son.list_changes import (
    get_commit_subjects_since,
    list_changes_command,
)
from tests.helpers import make_run_result


class TestGetCommitSubjectsSince(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_extracts_subjects(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'abc1234 First commit',
            'def5678 Second commit',
        ])
        subjects = get_commit_subjects_since('HEAD~1', '/workspace')
        self.assertEqual(subjects, ['First commit', 'Second commit'])
        mock_run.assert_called_once_with(
            ['git', 'log', '--format=%h %s', '--no-decorate',
             '--reverse', '--no-merges', 'HEAD~1..HEAD'],
            cwd='/workspace',
        )

    @mock.patch('git_p4son.git.run')
    def test_empty_log(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        subjects = get_commit_subjects_since('main', '/workspace')
        self.assertEqual(subjects, [])

    @mock.patch('git_p4son.git.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = RunError('git log failed')
        with self.assertRaises(RunError):
            get_commit_subjects_since('main', '/workspace')

    @mock.patch('git_p4son.git.run')
    def test_hash_only_line_fallback(self, mock_run):
        mock_run.return_value = make_run_result(stdout=['abc1234'])
        subjects = get_commit_subjects_since('main', '/workspace')
        self.assertEqual(subjects, ['abc1234'])


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
