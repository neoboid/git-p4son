"""Tests for git_p4son.review module."""

import os
import unittest
from unittest import mock

from git_p4son.review import (
    _generate_todo,
    _get_commit_lines,
    review_command,
    sequence_editor_command,
)
from tests.helpers import make_run_result


class TestGenerateTodo(unittest.TestCase):
    def test_single_commit(self):
        commit_lines = ['abc1234 First commit']
        result = _generate_todo(commit_lines, 'my-feature', 'My feature', force=False)
        self.assertEqual(result, (
            "pick abc1234 First commit\n"
            "exec git p4son new my-feature --review -m 'My feature'\n"
        ))

    def test_multiple_commits(self):
        commit_lines = [
            'abc1234 First commit',
            'def5678 Second commit',
            'ghi9012 Third commit',
        ]
        result = _generate_todo(commit_lines, 'my-feature', 'My feature', force=False)
        self.assertEqual(result, (
            "pick abc1234 First commit\n"
            "exec git p4son new my-feature --review -m 'My feature' --sleep 5\n"
            "pick def5678 Second commit\n"
            "exec git p4son update my-feature --shelve --sleep 5\n"
            "pick ghi9012 Third commit\n"
            "exec git p4son update my-feature --shelve\n"
        ))

    def test_force_flag(self):
        commit_lines = ['abc1234 First commit']
        result = _generate_todo(commit_lines, 'my-feature', 'My feature', force=True)
        self.assertEqual(result, (
            "pick abc1234 First commit\n"
            "exec git p4son new my-feature --review -m 'My feature' --force\n"
        ))

    def test_message_with_quotes(self):
        commit_lines = ['abc1234 First commit']
        result = _generate_todo(commit_lines, 'feat', "It's a feature", force=False)
        # shlex.quote wraps in quotes and escapes the apostrophe
        self.assertIn('exec git p4son new feat --review -m', result)
        # The result should be shell-safe (shlex.quote handles escaping)
        self.assertIn("It", result)
        self.assertIn("a feature", result)

    def test_alias_with_special_chars(self):
        commit_lines = ['abc1234 First commit']
        result = _generate_todo(commit_lines, 'my feature', 'msg', force=False)
        self.assertIn("'my feature'", result)


class TestGetCommitLines(unittest.TestCase):
    @mock.patch('git_p4son.review.run')
    def test_returns_lines(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'abc1234 First commit',
            'def5678 Second commit',
        ])
        rc, lines = _get_commit_lines('main', '/workspace')
        self.assertEqual(rc, 0)
        self.assertEqual(lines, ['abc1234 First commit', 'def5678 Second commit'])
        mock_run.assert_called_once_with(
            ['git', 'log', '--oneline', '--reverse', 'main..HEAD'],
            cwd='/workspace',
        )

    @mock.patch('git_p4son.review.run')
    def test_failure(self, mock_run):
        mock_run.return_value = make_run_result(returncode=128)
        rc, lines = _get_commit_lines('main', '/workspace')
        self.assertEqual(rc, 128)
        self.assertEqual(lines, [])


class TestReviewCommand(unittest.TestCase):
    @mock.patch('git_p4son.review.subprocess.run')
    @mock.patch('git_p4son.review.run')
    @mock.patch('git_p4son.review.ensure_workspace', return_value='/workspace')
    def test_success(self, _mock_ws, mock_run, mock_subprocess_run):
        mock_run.return_value = make_run_result(stdout=[
            'abc1234 First commit',
            'def5678 Second commit',
        ])
        mock_subprocess_run.return_value = mock.Mock(returncode=0)

        args = mock.Mock(
            alias='my-feature',
            message='My feature',
            base_branch='main',
            force=False,
            dry_run=False,
        )

        with mock.patch('os.path.exists', return_value=False):
            with mock.patch('os.makedirs'):
                with mock.patch('builtins.open', mock.mock_open()):
                    rc = review_command(args)

        self.assertEqual(rc, 0)
        # Verify git rebase was called with GIT_SEQUENCE_EDITOR
        mock_subprocess_run.assert_called_once()
        call_args = mock_subprocess_run.call_args
        self.assertEqual(call_args[0][0], ['git', 'rebase', '-i', 'main'])
        self.assertEqual(
            call_args[1]['env']['GIT_SEQUENCE_EDITOR'],
            'git p4son _sequence-editor',
        )

    @mock.patch('git_p4son.review.run')
    @mock.patch('git_p4son.review.ensure_workspace', return_value='/workspace')
    def test_no_commits(self, _mock_ws, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        args = mock.Mock(
            alias='my-feature',
            message='My feature',
            base_branch='main',
            force=False,
            dry_run=False,
        )
        with mock.patch('os.path.exists', return_value=False):
            rc = review_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.review.run')
    @mock.patch('git_p4son.review.ensure_workspace', return_value='/workspace')
    def test_existing_alias_without_force(self, _mock_ws, mock_run):
        args = mock.Mock(
            alias='my-feature',
            message='My feature',
            base_branch='main',
            force=False,
            dry_run=False,
        )
        with mock.patch('os.path.exists', return_value=True):
            rc = review_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.review.run')
    @mock.patch('git_p4son.review.ensure_workspace', return_value='/workspace')
    def test_dry_run_prints_todo(self, _mock_ws, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'abc1234 First commit',
            'def5678 Second commit',
        ])
        args = mock.Mock(
            alias='my-feature',
            message='My feature',
            base_branch='main',
            force=False,
            dry_run=True,
        )
        rc = review_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.review.subprocess.run')
    @mock.patch('git_p4son.review.run')
    @mock.patch('git_p4son.review.ensure_workspace', return_value='/workspace')
    def test_rebase_failure(self, _mock_ws, mock_run, mock_subprocess_run):
        mock_run.return_value = make_run_result(stdout=[
            'abc1234 First commit',
        ])
        mock_subprocess_run.return_value = mock.Mock(returncode=1)

        args = mock.Mock(
            alias='my-feature',
            message='My feature',
            base_branch='main',
            force=False,
            dry_run=False,
        )

        with mock.patch('os.path.exists', return_value=False):
            with mock.patch('os.makedirs'):
                with mock.patch('builtins.open', mock.mock_open()):
                    with mock.patch('os.remove'):
                        rc = review_command(args)

        self.assertEqual(rc, 1)


class TestSequenceEditorCommand(unittest.TestCase):
    @mock.patch('git_p4son.review.subprocess.run')
    @mock.patch('git_p4son.review.ensure_workspace', return_value='/workspace')
    def test_success(self, _mock_ws, mock_subprocess_run):
        todo_content = "pick abc First\nexec git p4son new feat --review -m 'msg'\n"

        # First call: git var GIT_EDITOR
        # Second call: editor
        mock_subprocess_run.side_effect = [
            mock.Mock(returncode=0, stdout='vim\n'),
            mock.Mock(returncode=0),
        ]

        args = mock.Mock(filename='/tmp/git-rebase-todo')
        todo_file = os.path.join('/workspace', '.git-p4son', 'reviews', 'todo')

        with mock.patch('os.path.exists', return_value=True):
            with mock.patch('builtins.open', mock.mock_open(read_data=todo_content)):
                rc = sequence_editor_command(args)

        self.assertEqual(rc, 0)
        # Verify git var GIT_EDITOR was called
        first_call = mock_subprocess_run.call_args_list[0]
        self.assertEqual(first_call[0][0], ['git', 'var', 'GIT_EDITOR'])
        # Verify editor was called with the filename
        second_call = mock_subprocess_run.call_args_list[1]
        self.assertEqual(second_call[0][0], ['vim', '/tmp/git-rebase-todo'])

    @mock.patch('git_p4son.review.ensure_workspace', return_value='/workspace')
    def test_missing_todo_file(self, _mock_ws):
        args = mock.Mock(filename='/tmp/git-rebase-todo')
        with mock.patch('os.path.exists', return_value=False):
            rc = sequence_editor_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.review.subprocess.run')
    @mock.patch('git_p4son.review.ensure_workspace', return_value='/workspace')
    def test_editor_with_args(self, _mock_ws, mock_subprocess_run):
        """Editor commands like 'code --wait' should be split properly."""
        todo_content = "pick abc First\n"
        mock_subprocess_run.side_effect = [
            mock.Mock(returncode=0, stdout='code --wait\n'),
            mock.Mock(returncode=0),
        ]

        args = mock.Mock(filename='/tmp/git-rebase-todo')
        with mock.patch('os.path.exists', return_value=True):
            with mock.patch('builtins.open', mock.mock_open(read_data=todo_content)):
                rc = sequence_editor_command(args)

        self.assertEqual(rc, 0)
        second_call = mock_subprocess_run.call_args_list[1]
        self.assertEqual(second_call[0][0], ['code', '--wait', '/tmp/git-rebase-todo'])


if __name__ == '__main__':
    unittest.main()
