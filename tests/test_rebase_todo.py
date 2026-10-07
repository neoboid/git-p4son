"""Tests for git_p4son.rebase_todo module."""

import os
import unittest
from unittest import mock

from git_p4son import CONFIG_DIR
from git_p4son.rebase_todo import pick_line, sequence_editor_command


class TestPickLine(unittest.TestCase):
    def test_pick_line(self):
        self.assertEqual(pick_line('abc1234 First commit'),
                         'pick abc1234 First commit')

    def test_commit_without_subject(self):
        self.assertEqual(pick_line('abc1234'), 'pick abc1234 ')


class TestSequenceEditorCommand(unittest.TestCase):
    @mock.patch('git_p4son.rebase_todo.subprocess.run')
    def test_success(self, mock_subprocess_run):
        todo_content = "pick abc First\nexec git p4son new feat --review -m 'msg'\n"

        # First call: git var GIT_EDITOR
        # Second call: editor
        mock_subprocess_run.side_effect = [
            mock.Mock(returncode=0, stdout='vim\n'),
            mock.Mock(returncode=0),
        ]

        args = mock.Mock(filename='/tmp/git-rebase-todo',
                         workspace_dir='/workspace', no_edit=False)
        todo_file = os.path.join('/workspace', CONFIG_DIR, 'reviews', 'todo')

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

    @mock.patch('git_p4son.rebase_todo.subprocess.run')
    def test_no_edit(self, mock_subprocess_run):
        """Writes our todo over git's without opening an editor."""
        todo_content = "pick abc First\nexec git p4son new feat --review -m 'msg'\n"
        args = mock.Mock(filename='/tmp/git-rebase-todo',
                         workspace_dir='/workspace', no_edit=True)

        opener = mock.mock_open(read_data=todo_content)
        with mock.patch('os.path.exists', return_value=True):
            with mock.patch('builtins.open', opener):
                rc = sequence_editor_command(args)

        self.assertEqual(rc, 0)
        mock_subprocess_run.assert_not_called()
        opener.assert_any_call('/tmp/git-rebase-todo', 'w')
        opener().write.assert_any_call(todo_content)

    def test_missing_todo_file(self):
        args = mock.Mock(filename='/tmp/git-rebase-todo',
                         workspace_dir='/workspace', no_edit=False)
        with mock.patch('os.path.exists', return_value=False):
            rc = sequence_editor_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.rebase_todo.subprocess.run')
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
                         workspace_dir='/workspace', no_edit=False)
        todo_file = os.path.join('/workspace', CONFIG_DIR, 'reviews', 'todo')

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

    @mock.patch('git_p4son.rebase_todo.subprocess.run')
    def test_editor_with_args(self, mock_subprocess_run):
        """Editor commands like 'code --wait' should be split properly."""
        todo_content = "pick abc First\n"
        mock_subprocess_run.side_effect = [
            mock.Mock(returncode=0, stdout='code --wait\n'),
            mock.Mock(returncode=0),
        ]

        args = mock.Mock(filename='/tmp/git-rebase-todo',
                         workspace_dir='/workspace', no_edit=False)
        with mock.patch('os.path.exists', return_value=True):
            with mock.patch('builtins.open', mock.mock_open(read_data=todo_content)):
                rc = sequence_editor_command(args)

        self.assertEqual(rc, 0)
        second_call = mock_subprocess_run.call_args_list[1]
        self.assertEqual(second_call[0][0], [
                         'code', '--wait', '/tmp/git-rebase-todo'])


if __name__ == '__main__':
    unittest.main()
