"""Tests for git_p4son.rebase_todo module."""

import os
import tempfile
import unittest
from unittest import mock

from git_p4son import CONFIG_DIR
from git_p4son.rebase_todo import (
    MESSAGE_FILE, _build_todo, rebase_per_commit, _run_todo_rebase,
    sequence_editor_command,
)


class TestBuildTodo(unittest.TestCase):
    def test_first_command_then_the_other(self):
        result = _build_todo(['abc First', 'def Second', 'ghi Third'],
                             'new a', 'update a', with_message=False, sleep=None)
        self.assertEqual(result, (
            "pick abc First\n"
            "exec git p4son new a\n"
            "pick def Second\n"
            "exec git p4son update a\n"
            "pick ghi Third\n"
            "exec git p4son update a\n"
        ))

    def test_message_file_on_first_step_only(self):
        result = _build_todo(['abc First', 'def Second'], 'update 100',
                             'update 100', with_message=True, sleep=None)
        self.assertEqual(result, (
            "pick abc First\n"
            "exec git p4son update 100 -F .git-p4son/reviews/message\n"
            "pick def Second\n"
            "exec git p4son update 100\n"
        ))

    def test_sleeps_between_steps_but_not_after_the_last(self):
        result = _build_todo(['abc First', 'def Second'], 'new a', 'update a',
                             with_message=True, sleep=5)
        self.assertEqual(result, (
            "pick abc First\n"
            "exec git p4son new a -F .git-p4son/reviews/message --sleep 5\n"
            "pick def Second\n"
            "exec git p4son update a\n"
        ))

    def test_single_commit_does_not_sleep(self):
        self.assertEqual(_build_todo(['abc First'], 'new a', 'update a', sleep=5, with_message=False),
                         "pick abc First\nexec git p4son new a\n")


@mock.patch('git_p4son.rebase_todo._run_todo_rebase', return_value=0)
@mock.patch('git_p4son.rebase_todo.get_commit_lines_since')
class TestRebasePerCommit(unittest.TestCase):
    def _rebase(self, message=None):
        return rebase_per_commit('new a', 'update a', 'main', '/ws',
                                 message=message, sleep=None,
                                 edit_todo=False, dry_run=False)

    def test_passes_the_todo_and_message_to_the_rebase(self, mock_commits,
                                                       mock_rebase):
        mock_commits.return_value = ['abc First']
        self.assertEqual(self._rebase(message='Title'), 0)
        mock_commits.assert_called_once_with('main', '/ws')
        todo, base, ws = mock_rebase.call_args.args
        self.assertEqual(
            todo, 'pick abc First\n'
            'exec git p4son new a -F .git-p4son/reviews/message\n')
        self.assertEqual((base, ws), ('main', '/ws'))
        self.assertEqual(mock_rebase.call_args.kwargs['message'], 'Title')

    def test_no_commits(self, mock_commits, mock_rebase):
        mock_commits.return_value = []
        self.assertEqual(self._rebase(), 1)
        mock_rebase.assert_not_called()


@mock.patch('git_p4son.rebase_todo.subprocess.run',
            return_value=mock.Mock(returncode=0))
class TestRunTodoRebase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch('git_p4son.rebase_todo.check_git_workspace_clean',
                             return_value=True)
        self.mock_clean = patcher.start()
        self.addCleanup(patcher.stop)

    def test_refuses_dirty_workspace(self, mock_run):
        self.mock_clean.return_value = False
        with tempfile.TemporaryDirectory() as ws:
            rc = _run_todo_rebase('pick abc First\n', 'main', ws,
                                  edit_todo=False, message='Title', dry_run=False)
            self.assertEqual(rc, 1)
            self.assertEqual(os.listdir(ws), [])
        mock_run.assert_not_called()

    def test_message_saved_and_left_in_place(self, _run):
        with tempfile.TemporaryDirectory() as ws:
            rc = _run_todo_rebase('pick abc First\n', 'main', ws,
                                  edit_todo=False, message='Title\n\nBody', dry_run=False)
            self.assertEqual(rc, 0)
            with open(os.path.join(ws, MESSAGE_FILE), encoding='utf-8') as f:
                self.assertEqual(f.read(), 'Title\n\nBody')
            self.assertFalse(os.path.exists(
                os.path.join(ws, CONFIG_DIR, 'reviews', 'todo')))

    def test_dry_run_prints_and_writes_nothing(self, mock_run):
        with tempfile.TemporaryDirectory() as ws, \
                mock.patch('git_p4son.rebase_todo.log') as mock_log:
            rc = _run_todo_rebase('pick abc First\n', 'main', ws,
                                  edit_todo=False, message='Title',
                                  dry_run=True)
            self.assertEqual(rc, 0)
            self.assertEqual(os.listdir(ws), [])
        mock_run.assert_not_called()
        printed = [c.args[0] for c in mock_log.info.call_args_list]
        self.assertIn('pick abc First\n', printed)
        self.assertIn('Title', printed)

    def test_no_message_writes_no_file(self, _run):
        with tempfile.TemporaryDirectory() as ws:
            _run_todo_rebase('pick abc First\n', 'main', ws,
                             edit_todo=False, message=None, dry_run=False)
            self.assertFalse(os.path.exists(os.path.join(ws, MESSAGE_FILE)))


class TestSequenceEditorCommand(unittest.TestCase):
    @mock.patch('git_p4son.rebase_todo.subprocess.run')
    def test_success(self, mock_subprocess_run):
        todo_content = "pick abc First\nexec git p4son new feat --review -m 'msg'\n"

        # git var GIT_EDITOR, then the editor
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
        first_call = mock_subprocess_run.call_args_list[0]
        self.assertEqual(first_call[0][0], ['git', 'var', 'GIT_EDITOR'])
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
        self.assertIn("pick abc First", full_output)
        self.assertIn("exec git p4son new feat", full_output)
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
