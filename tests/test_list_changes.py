"""Tests for git_p4son.list_changes module."""

import unittest
from unittest import mock

from git_p4son.list_changes import (
    get_commit_subjects_since,
    get_enumerated_change_description_since,
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
        rc, subjects = get_commit_subjects_since('HEAD~1', '/workspace')
        self.assertEqual(rc, 0)
        self.assertEqual(subjects, ['First commit', 'Second commit'])
        mock_run.assert_called_once_with(
            ['git', 'log', '--oneline', '--reverse', 'HEAD~1..HEAD'],
            cwd='/workspace',
        )

    @mock.patch('git_p4son.list_changes.run')
    def test_empty_log(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        rc, subjects = get_commit_subjects_since('main', '/workspace')
        self.assertEqual(rc, 0)
        self.assertEqual(subjects, [])

    @mock.patch('git_p4son.list_changes.run')
    def test_failure(self, mock_run):
        mock_run.return_value = make_run_result(returncode=128)
        rc, subjects = get_commit_subjects_since('main', '/workspace')
        self.assertEqual(rc, 128)
        self.assertIsNone(subjects)

    @mock.patch('git_p4son.list_changes.run')
    def test_hash_only_line_fallback(self, mock_run):
        mock_run.return_value = make_run_result(stdout=['abc1234'])
        rc, subjects = get_commit_subjects_since('main', '/workspace')
        self.assertEqual(rc, 0)
        self.assertEqual(subjects, ['abc1234'])


class TestGetEnumeratedChangeDescriptionSince(unittest.TestCase):
    @mock.patch('git_p4son.list_changes.run')
    def test_enumerated_output(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'a111111 Add feature',
            'b222222 Fix bug',
            'c333333 Update docs',
        ])
        rc, desc = get_enumerated_change_description_since('main', '/ws')
        self.assertEqual(rc, 0)
        self.assertEqual(desc, '1. Add feature\n2. Fix bug\n3. Update docs')

    @mock.patch('git_p4son.list_changes.run')
    def test_no_commits_returns_none(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        rc, desc = get_enumerated_change_description_since('main', '/ws')
        self.assertEqual(rc, 0)
        self.assertIsNone(desc)


class TestListChangesCommand(unittest.TestCase):
    @mock.patch('git_p4son.list_changes.ensure_workspace', return_value='/ws')
    @mock.patch('git_p4son.list_changes.run')
    def test_success(self, mock_run, _mock_ws):
        mock_run.return_value = make_run_result(stdout=[
            'aaa Fix something',
        ])
        args = mock.Mock(base_branch='HEAD~1')
        rc = list_changes_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.list_changes.ensure_workspace', return_value='/ws')
    @mock.patch('git_p4son.list_changes.run')
    def test_no_changes(self, mock_run, _mock_ws):
        mock_run.return_value = make_run_result(stdout=[])
        args = mock.Mock(base_branch='HEAD~1')
        rc = list_changes_command(args)
        self.assertEqual(rc, 0)


if __name__ == '__main__':
    unittest.main()
