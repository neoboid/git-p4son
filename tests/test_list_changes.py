"""Tests for git_p4son.list_changes module."""

import unittest
from unittest import mock

from git_p4son.common import CommandError
from git_p4son.list_changes import (
    get_commit_subjects_since,
    get_enumerated_change_description_since,
    get_enumerated_commit_lines_since,
    list_changes_command,
)
from tests.helpers import make_run_result


class TestGetCommitSubjectsSince(unittest.TestCase):
    @mock.patch('git_p4son.list_changes.run')
    def test_extracts_subjects(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'abc1234 First commit',
            'def5678 Second commit',
        ])
        subjects = get_commit_subjects_since('HEAD~1', '/workspace')
        self.assertEqual(subjects, ['First commit', 'Second commit'])
        mock_run.assert_called_once_with(
            ['git', 'log', '--oneline', '--reverse', 'HEAD~1..HEAD'],
            cwd='/workspace',
        )

    @mock.patch('git_p4son.list_changes.run')
    def test_empty_log(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        subjects = get_commit_subjects_since('main', '/workspace')
        self.assertEqual(subjects, [])

    @mock.patch('git_p4son.list_changes.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = CommandError('git log failed')
        with self.assertRaises(CommandError):
            get_commit_subjects_since('main', '/workspace')

    @mock.patch('git_p4son.list_changes.run')
    def test_hash_only_line_fallback(self, mock_run):
        mock_run.return_value = make_run_result(stdout=['abc1234'])
        subjects = get_commit_subjects_since('main', '/workspace')
        self.assertEqual(subjects, ['abc1234'])


class TestGetEnumeratedCommitLinesSince(unittest.TestCase):
    @mock.patch('git_p4son.list_changes.run')
    def test_returns_enumerated_lines(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'a111111 Add feature',
            'b222222 Fix bug',
            'c333333 Update docs',
        ])
        lines = get_enumerated_commit_lines_since('main', '/ws')
        self.assertEqual(lines, ['1. Add feature', '2. Fix bug', '3. Update docs'])

    @mock.patch('git_p4son.list_changes.run')
    def test_no_commits_returns_empty_list(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        lines = get_enumerated_commit_lines_since('main', '/ws')
        self.assertEqual(lines, [])

    @mock.patch('git_p4son.list_changes.run')
    def test_start_number_parameter(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'a111111 New commit A',
            'b222222 New commit B',
        ])
        lines = get_enumerated_commit_lines_since('main', '/ws', start_number=4)
        self.assertEqual(lines, ['4. New commit A', '5. New commit B'])

    @mock.patch('git_p4son.list_changes.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = CommandError('git log failed')
        with self.assertRaises(CommandError):
            get_enumerated_commit_lines_since('main', '/ws')


class TestGetEnumeratedChangeDescriptionSince(unittest.TestCase):
    @mock.patch('git_p4son.list_changes.run')
    def test_enumerated_output(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'a111111 Add feature',
            'b222222 Fix bug',
            'c333333 Update docs',
        ])
        desc = get_enumerated_change_description_since('main', '/ws')
        self.assertEqual(desc, '1. Add feature\n2. Fix bug\n3. Update docs')

    @mock.patch('git_p4son.list_changes.run')
    def test_no_commits_returns_none(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        desc = get_enumerated_change_description_since('main', '/ws')
        self.assertIsNone(desc)

    @mock.patch('git_p4son.list_changes.run')
    def test_start_number_parameter(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'a111111 New commit A',
            'b222222 New commit B',
        ])
        desc = get_enumerated_change_description_since('main', '/ws', start_number=4)
        self.assertEqual(desc, '4. New commit A\n5. New commit B')


class TestListChangesCommand(unittest.TestCase):
    @mock.patch('git_p4son.list_changes.run')
    def test_success(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'aaa Fix something',
        ])
        args = mock.Mock(base_branch='HEAD~1', workspace_dir='/ws')
        rc = list_changes_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.list_changes.run')
    def test_no_changes(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        args = mock.Mock(base_branch='HEAD~1', workspace_dir='/ws')
        rc = list_changes_command(args)
        self.assertEqual(rc, 0)


if __name__ == '__main__':
    unittest.main()
