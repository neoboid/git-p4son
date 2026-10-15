"""Tests for git_p4son.review module."""

import unittest
from unittest import mock

from git_p4son.common import CommandError
from git_p4son.review import review_command, _step_commands
from tests.helpers import cli_args, start_patch


class TestStepCommands(unittest.TestCase):
    def test_new_then_update(self):
        self.assertEqual(_step_commands('my-feature', False, False),
                         ('new my-feature --review',
                          'update my-feature --shelve'))

    def test_force_flag(self):
        self.assertEqual(_step_commands('my-feature', True, False)[0],
                         'new my-feature --review --force')

    def test_no_commit_list(self):
        self.assertEqual(_step_commands('feat', False, True),
                         ('new feat --review --no-commit-list',
                          'update feat --shelve --no-commit-list'))

    def test_alias_with_special_chars(self):
        new_cmd, update_cmd = _step_commands('my feature', False, False)
        self.assertIn("'my feature'", new_cmd)
        self.assertIn("'my feature'", update_cmd)


def _args(**overrides):
    return cli_args(['review', 'my-feature', '-m', 'My feature', '-b', 'main'],
                    workspace_dir='/workspace', **overrides)


class TestReviewCommand(unittest.TestCase):
    def setUp(self):
        start_patch(self, 'git_p4son.rebase_todo.check_git_workspace_clean',
                    return_value=True)

    @mock.patch('git_p4son.rebase_todo.subprocess.run')
    @mock.patch('git_p4son.rebase_todo.resolve_editor', return_value='vim')
    @mock.patch('git_p4son.rebase_todo.get_commit_lines_since')
    def test_success(self, mock_run, mock_resolve_editor, mock_subprocess_run):
        mock_run.return_value = [
            'abc1234 First commit',
            'def5678 Second commit',
        ]
        mock_subprocess_run.return_value = mock.Mock(returncode=0)

        args = _args()

        with mock.patch('os.path.exists', return_value=False):
            with mock.patch('os.makedirs'):
                with mock.patch('builtins.open', mock.mock_open()):
                    rc = review_command(args)

        self.assertEqual(rc, 0)
        mock_subprocess_run.assert_called_once()
        call_args = mock_subprocess_run.call_args
        self.assertEqual(call_args[0][0], ['git', 'rebase', '-i', 'main'])
        self.assertEqual(
            call_args[1]['env']['GIT_SEQUENCE_EDITOR'],
            'git-p4son _sequence-editor',
        )

    @mock.patch('git_p4son.rebase_todo.subprocess.run')
    @mock.patch('git_p4son.rebase_todo.resolve_editor', return_value=None)
    @mock.patch('git_p4son.rebase_todo.get_commit_lines_since')
    def test_no_edit_todo(self, mock_run, mock_resolve_editor,
                          mock_subprocess_run):
        """Accepts the todo without an editor, so none needs configuring."""
        mock_run.return_value = ['abc1234 First commit']
        mock_subprocess_run.return_value = mock.Mock(returncode=0)

        args = _args(no_edit_todo=True)

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

    @mock.patch('git_p4son.rebase_todo.resolve_editor', return_value='vim')
    @mock.patch('git_p4son.rebase_todo.get_commit_lines_since')
    def test_multiline_message_accepted(self, mock_run, _editor):
        """The message goes through a file, so -m may span lines too."""
        mock_run.return_value = ['abc1234 First commit']
        args = _args(message='Line one\nLine two', dry_run=True)
        rc = review_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.rebase_todo.resolve_editor', return_value='vim')
    @mock.patch('git_p4son.rebase_todo.get_commit_lines_since')
    def test_no_commits(self, mock_run, mock_resolve_editor):
        mock_run.return_value = []
        args = _args()
        with mock.patch('os.path.exists', return_value=False):
            with self.assertRaisesRegex(CommandError, 'No commits found'):
                review_command(args)

    @mock.patch('git_p4son.rebase_todo.subprocess.run')
    @mock.patch('git_p4son.rebase_todo.resolve_editor', return_value=None)
    @mock.patch('git_p4son.rebase_todo.get_commit_lines_since',
                return_value=['abc1234 First commit'])
    def test_no_editor_fails_before_the_rebase(self, _commits, _editor,
                                               mock_subprocess_run):
        args = _args()
        with mock.patch('os.path.exists', return_value=False):
            with self.assertRaisesRegex(CommandError, 'No git editor configured'):
                review_command(args)
        mock_subprocess_run.assert_not_called()

    @mock.patch('git_p4son.rebase_todo.get_commit_lines_since')
    def test_existing_alias_without_force(self, mock_run):
        args = _args()
        with mock.patch('os.path.exists', return_value=True), \
                self.assertRaisesRegex(CommandError, 'already exists'):
            review_command(args)

    @mock.patch('git_p4son.rebase_todo.resolve_editor', return_value='vim')
    @mock.patch('git_p4son.rebase_todo.get_commit_lines_since')
    def test_dry_run_prints_todo(self, mock_run, mock_resolve_editor):
        mock_run.return_value = [
            'abc1234 First commit',
            'def5678 Second commit',
        ]
        args = _args(dry_run=True)
        rc = review_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.rebase_todo.subprocess.run')
    @mock.patch('git_p4son.rebase_todo.resolve_editor', return_value='vim')
    @mock.patch('git_p4son.rebase_todo.get_commit_lines_since')
    def test_rebase_failure(self, mock_run, mock_resolve_editor,
                            mock_subprocess_run):
        mock_run.return_value = [
            'abc1234 First commit',
        ]
        mock_subprocess_run.return_value = mock.Mock(returncode=1)

        args = _args()

        with mock.patch('os.path.exists', return_value=False):
            with mock.patch('os.makedirs'):
                with mock.patch('builtins.open', mock.mock_open()):
                    with mock.patch('os.remove'):
                        with self.assertRaisesRegex(CommandError, 'Rebase did not complete'):
                            review_command(args)


if __name__ == '__main__':
    unittest.main()
