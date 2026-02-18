"""Tests for git_p4son.review module."""

import os
import unittest
from unittest import mock

from git_p4son.common import CommandError
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
        lines = _get_commit_lines('main', '/workspace')
        self.assertEqual(lines, ['abc1234 First commit', 'def5678 Second commit'])
        mock_run.assert_called_once_with(
            ['git', 'log', '--oneline', '--reverse', 'main..HEAD'],
            cwd='/workspace',
        )

    @mock.patch('git_p4son.review.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = CommandError('git log failed')
        with self.assertRaises(CommandError):
            _get_commit_lines('main', '/workspace')


class TestReviewCommand(unittest.TestCase):
    @mock.patch('git_p4son.review.subprocess.run')
    @mock.patch('git_p4son.review.run')
    def test_success(self, mock_run, mock_subprocess_run):
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
            workspace_dir='/workspace',
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
    def test_no_commits(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        args = mock.Mock(
            alias='my-feature',
            message='My feature',
            base_branch='main',
            force=False,
            dry_run=False,
            workspace_dir='/workspace',
        )
        with mock.patch('os.path.exists', return_value=False):
            rc = review_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.review.run')
    def test_existing_alias_without_force(self, mock_run):
        args = mock.Mock(
            alias='my-feature',
            message='My feature',
            base_branch='main',
            force=False,
            dry_run=False,
            workspace_dir='/workspace',
        )
        with mock.patch('os.path.exists', return_value=True):
            rc = review_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.review.run')
    def test_dry_run_prints_todo(self, mock_run):
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
            workspace_dir='/workspace',
        )
        rc = review_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.review.subprocess.run')
    @mock.patch('git_p4son.review.run')
    def test_rebase_failure(self, mock_run, mock_subprocess_run):
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
            workspace_dir='/workspace',
        )

        with mock.patch('os.path.exists', return_value=False):
            with mock.patch('os.makedirs'):
                with mock.patch('builtins.open', mock.mock_open()):
                    with mock.patch('os.remove'):
                        rc = review_command(args)

        self.assertEqual(rc, 1)


class TestSequenceEditorCommand(unittest.TestCase):
    @mock.patch('git_p4son.review.subprocess.run')
    def test_success(self, mock_subprocess_run):
        todo_content = "pick abc First\nexec git p4son new feat --review -m 'msg'\n"

        # First call: git var GIT_EDITOR
        # Second call: editor
        mock_subprocess_run.side_effect = [
            mock.Mock(returncode=0, stdout='vim\n'),
            mock.Mock(returncode=0),
        ]

        args = mock.Mock(filename='/tmp/git-rebase-todo',
                         workspace_dir='/workspace')
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

    def test_missing_todo_file(self):
        args = mock.Mock(filename='/tmp/git-rebase-todo',
                         workspace_dir='/workspace')
        with mock.patch('os.path.exists', return_value=False):
            rc = sequence_editor_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.review.subprocess.run')
    def test_preserves_git_comments(self, mock_subprocess_run):
        """Comment lines from git's original todo file are preserved."""
        git_original = (
            "pick abc1234 First commit\n"
            "\n"
            "# Rebase abc1234..abc1234 onto abc1234 (1 command)\n"
            "#\n"
            "# Commands:\n"
            "# p, pick <commit> = use commit\n"
        )
        our_todo = "pick abc First\nexec git p4son new feat --review -m 'msg'\n"

        mock_subprocess_run.side_effect = [
            mock.Mock(returncode=0, stdout='vim\n'),
            mock.Mock(returncode=0),
        ]

        args = mock.Mock(filename='/tmp/git-rebase-todo',
                         workspace_dir='/workspace')
        todo_file = os.path.join('/workspace', '.git-p4son', 'reviews', 'todo')

        written = []

        def open_side_effect(path, mode='r'):
            if path == '/tmp/git-rebase-todo' and mode == 'r':
                return mock.mock_open(read_data=git_original)()
            elif path == todo_file and mode == 'r':
                return mock.mock_open(read_data=our_todo)()
            elif path == '/tmp/git-rebase-todo' and mode == 'w':
                m = mock.MagicMock()
                m.__enter__ = mock.Mock(return_value=m)
                m.__exit__ = mock.Mock(return_value=False)
                m.write = lambda data: written.append(data)
                m.writelines = lambda lines: written.extend(lines)
                return m
            return mock.mock_open()()

        with mock.patch('os.path.exists', return_value=True):
            with mock.patch('builtins.open', side_effect=open_side_effect):
                rc = sequence_editor_command(args)

        self.assertEqual(rc, 0)
        full_output = ''.join(written)
        # Our todo content is included
        self.assertIn("pick abc First", full_output)
        self.assertIn("exec git p4son new feat", full_output)
        # Git's comment lines are preserved
        self.assertIn("# Commands:", full_output)
        self.assertIn("# p, pick <commit> = use commit", full_output)
        # Non-comment lines from git's original are NOT included
        self.assertNotIn("pick abc1234 First commit", full_output)

    @mock.patch('git_p4son.review.subprocess.run')
    def test_editor_with_args(self, mock_subprocess_run):
        """Editor commands like 'code --wait' should be split properly."""
        todo_content = "pick abc First\n"
        mock_subprocess_run.side_effect = [
            mock.Mock(returncode=0, stdout='code --wait\n'),
            mock.Mock(returncode=0),
        ]

        args = mock.Mock(filename='/tmp/git-rebase-todo',
                         workspace_dir='/workspace')
        with mock.patch('os.path.exists', return_value=True):
            with mock.patch('builtins.open', mock.mock_open(read_data=todo_content)):
                rc = sequence_editor_command(args)

        self.assertEqual(rc, 0)
        second_call = mock_subprocess_run.call_args_list[1]
        self.assertEqual(second_call[0][0], ['code', '--wait', '/tmp/git-rebase-todo'])


if __name__ == '__main__':
    unittest.main()
