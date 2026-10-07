"""Tests for git_p4son.review module."""

import os
import unittest
from unittest import mock

from git_p4son.common import CommandError, RunError
from git_p4son.git import get_commit_lines_since
from git_p4son.review import (
    _generate_todo,
    review_command,
)
from tests.helpers import make_run_result


class TestGenerateTodo(unittest.TestCase):
    def test_single_commit(self):
        commit_lines = ['abc1234 First commit']
        result = _generate_todo(
            commit_lines, 'my-feature', 'My feature', force=False)
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
        result = _generate_todo(
            commit_lines, 'my-feature', 'My feature', force=False)
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
        result = _generate_todo(
            commit_lines, 'my-feature', 'My feature', force=True)
        self.assertEqual(result, (
            "pick abc1234 First commit\n"
            "exec git p4son new my-feature --review -m 'My feature' --force\n"
        ))

    def test_message_with_quotes(self):
        commit_lines = ['abc1234 First commit']
        result = _generate_todo(commit_lines, 'feat',
                                "It's a feature", force=False)
        # shlex.quote wraps in quotes and escapes the apostrophe
        self.assertIn('exec git p4son new feat --review -m', result)
        # The result should be shell-safe (shlex.quote handles escaping)
        self.assertIn("It", result)
        self.assertIn("a feature", result)

    def test_message_file(self):
        commit_lines = ['abc1234 First commit']
        result = _generate_todo(commit_lines, 'feat', 'Title\n\nBody',
                                force=False, message_file='/tmp/my msg.txt')
        self.assertEqual(result, (
            "pick abc1234 First commit\n"
            "exec git p4son new feat --review -F '/tmp/my msg.txt'\n"
        ))

    def test_no_commit_list(self):
        commit_lines = ['abc1234 First commit', 'def5678 Second commit']
        result = _generate_todo(commit_lines, 'feat', 'msg', force=False,
                                no_commit_list=True)
        self.assertEqual(result, (
            "pick abc1234 First commit\n"
            "exec git p4son new feat --review -m msg --no-commit-list --sleep 5\n"
            "pick def5678 Second commit\n"
            "exec git p4son update feat --shelve --no-commit-list\n"
        ))

    def test_alias_with_special_chars(self):
        commit_lines = ['abc1234 First commit']
        result = _generate_todo(commit_lines, 'my feature', 'msg', force=False)
        self.assertIn("'my feature'", result)


class TestGetCommitLines(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_returns_lines(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'abc1234 First commit',
            'def5678 Second commit',
        ])
        lines = get_commit_lines_since('main', '/workspace')
        self.assertEqual(
            lines, ['abc1234 First commit', 'def5678 Second commit'])
        mock_run.assert_called_once_with(
            ['git', 'log', '--format=%h %s', '--no-decorate',
             '--reverse', '--no-merges', 'main..HEAD'],
            cwd='/workspace',
        )

    @mock.patch('git_p4son.git.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = RunError('git log failed')
        with self.assertRaises(RunError):
            get_commit_lines_since('main', '/workspace')


class TestReviewCommand(unittest.TestCase):
    @mock.patch('git_p4son.rebase_todo.subprocess.run')
    @mock.patch('git_p4son.review.resolve_editor', return_value='vim')
    @mock.patch('git_p4son.review.get_commit_lines_since')
    def test_success(self, mock_run, mock_resolve_editor, mock_subprocess_run):
        mock_run.return_value = [
            'abc1234 First commit',
            'def5678 Second commit',
        ]
        mock_subprocess_run.return_value = mock.Mock(returncode=0)

        args = mock.Mock(
            alias='my-feature',
            message='My feature',
            file=None,
            base_branch='main',
            force=False,
            dry_run=False,
            no_edit_todo=False,
            no_commit_list=False,
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
            'git-p4son _sequence-editor',
        )

    @mock.patch('git_p4son.rebase_todo.subprocess.run')
    @mock.patch('git_p4son.review.resolve_editor', return_value=None)
    @mock.patch('git_p4son.review.get_commit_lines_since')
    def test_no_edit_todo(self, mock_run, mock_resolve_editor,
                          mock_subprocess_run):
        """Accepts the todo without an editor, so none needs configuring."""
        mock_run.return_value = ['abc1234 First commit']
        mock_subprocess_run.return_value = mock.Mock(returncode=0)

        args = mock.Mock(
            alias='my-feature',
            message='My feature',
            file=None,
            base_branch='main',
            force=False,
            dry_run=False,
            no_edit_todo=True,
            no_commit_list=False,
            workspace_dir='/workspace',
        )

        with mock.patch('os.path.exists', return_value=False):
            with mock.patch('os.makedirs'):
                with mock.patch('builtins.open', mock.mock_open()):
                    rc = review_command(args)

        self.assertEqual(rc, 0)
        mock_resolve_editor.assert_not_called()
        self.assertEqual(
            mock_subprocess_run.call_args[1]['env']['GIT_SEQUENCE_EDITOR'],
            'git-p4son _sequence-editor --no-edit',
        )

    def test_multiline_message_rejected(self):
        """The rebase todo is line-based; an embedded newline in the
        message would split the exec line."""
        args = mock.Mock(
            alias='my-feature',
            message='Line one\nLine two',
            file=None,
            base_branch='main',
            force=False,
            dry_run=False,
            no_edit_todo=False,
            no_commit_list=False,
            workspace_dir='/workspace',
        )
        rc = review_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.review.resolve_editor', return_value='vim')
    @mock.patch('git_p4son.review.get_commit_lines_since')
    def test_multiline_message_from_file_accepted(self, mock_run, _editor):
        mock_run.return_value = ['abc1234 First commit']
        args = mock.Mock(
            alias='my-feature',
            message='Line one\nLine two',
            file='/workspace/msg.txt',
            base_branch='main',
            force=False,
            dry_run=True,
            no_edit_todo=False,
            no_commit_list=False,
            workspace_dir='/workspace',
        )
        with mock.patch('git_p4son.review.alias_exists', return_value=False):
            rc = review_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.review.resolve_editor', return_value='vim')
    @mock.patch('git_p4son.review.get_commit_lines_since')
    def test_no_commits(self, mock_run, mock_resolve_editor):
        mock_run.return_value = []
        args = mock.Mock(
            alias='my-feature',
            message='My feature',
            file=None,
            base_branch='main',
            force=False,
            dry_run=False,
            no_edit_todo=False,
            no_commit_list=False,
            workspace_dir='/workspace',
        )
        with mock.patch('os.path.exists', return_value=False):
            rc = review_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.review.resolve_editor', return_value=None)
    def test_no_editor_fails_early(self, mock_resolve_editor):
        args = mock.Mock(
            alias='my-feature',
            message='My feature',
            file=None,
            base_branch='main',
            force=False,
            dry_run=False,
            no_edit_todo=False,
            no_commit_list=False,
            workspace_dir='/workspace',
        )
        with mock.patch('os.path.exists', return_value=False):
            rc = review_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.review.get_commit_lines_since')
    def test_existing_alias_without_force(self, mock_run):
        args = mock.Mock(
            alias='my-feature',
            message='My feature',
            file=None,
            base_branch='main',
            force=False,
            dry_run=False,
            no_edit_todo=False,
            no_commit_list=False,
            workspace_dir='/workspace',
        )
        with mock.patch('os.path.exists', return_value=True):
            rc = review_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.review.resolve_editor', return_value='vim')
    @mock.patch('git_p4son.review.get_commit_lines_since')
    def test_dry_run_prints_todo(self, mock_run, mock_resolve_editor):
        mock_run.return_value = [
            'abc1234 First commit',
            'def5678 Second commit',
        ]
        args = mock.Mock(
            alias='my-feature',
            message='My feature',
            file=None,
            base_branch='main',
            force=False,
            dry_run=True,
            no_edit_todo=False,
            no_commit_list=False,
            workspace_dir='/workspace',
        )
        rc = review_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.rebase_todo.subprocess.run')
    @mock.patch('git_p4son.review.resolve_editor', return_value='vim')
    @mock.patch('git_p4son.review.get_commit_lines_since')
    def test_rebase_failure(self, mock_run, mock_resolve_editor,
                            mock_subprocess_run):
        mock_run.return_value = [
            'abc1234 First commit',
        ]
        mock_subprocess_run.return_value = mock.Mock(returncode=1)

        args = mock.Mock(
            alias='my-feature',
            message='My feature',
            file=None,
            base_branch='main',
            force=False,
            dry_run=False,
            no_edit_todo=False,
            no_commit_list=False,
            workspace_dir='/workspace',
        )

        with mock.patch('os.path.exists', return_value=False):
            with mock.patch('os.makedirs'):
                with mock.patch('builtins.open', mock.mock_open()):
                    with mock.patch('os.remove'):
                        rc = review_command(args)

        self.assertEqual(rc, 1)


if __name__ == '__main__':
    unittest.main()
