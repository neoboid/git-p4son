"""Tests for git_p4son.cli module."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from git_p4son.cli import (
    create_parser,
    main,
    _resolve_branch_keyword,
    run_command,
)
from git_p4son.common import CommandError, RunError
from git_p4son.log import log
from tests.helpers import captured_output


class TestCreateParser(unittest.TestCase):
    def setUp(self):
        self.parser = create_parser()

    def test_sync_command_basic(self):
        args = self.parser.parse_args(['sync', '12345'])
        self.assertEqual(args.command, 'sync')
        self.assertEqual(args.changelist, ['12345'])
        self.assertFalse(args.force)

    def test_writable_command_without_action(self):
        args = self.parser.parse_args(['writable'])
        self.assertEqual(args.command, 'writable')
        self.assertIsNone(args.writable_action)

    def test_writable_actions(self):
        for action in ('enable', 'disable', 'apply'):
            args = self.parser.parse_args(['writable', action])
            self.assertEqual(args.writable_action, action)

    def test_sync_split_users_command_without_action(self):
        args = self.parser.parse_args(['sync-split-users'])
        self.assertEqual(args.command, 'sync-split-users')
        self.assertIsNone(args.split_users_action)

    def test_sync_split_users_list(self):
        args = self.parser.parse_args(['sync-split-users', 'list'])
        self.assertEqual(args.split_users_action, 'list')

    def test_sync_split_users_add(self):
        args = self.parser.parse_args(
            ['sync-split-users', 'add', 'alice', 'bob'])
        self.assertEqual(args.split_users_action, 'add')
        self.assertEqual(args.names, ['alice', 'bob'])
        self.assertFalse(args.me)

    def test_sync_split_users_add_me(self):
        args = self.parser.parse_args(['sync-split-users', 'add', '--me'])
        self.assertEqual(args.names, [])
        self.assertTrue(args.me)

    def test_sync_split_users_delete(self):
        args = self.parser.parse_args(
            ['sync-split-users', 'delete', '--me', 'bob'])
        self.assertEqual(args.split_users_action, 'delete')
        self.assertEqual(args.names, ['bob'])
        self.assertTrue(args.me)

    def test_sync_command_no_changelist(self):
        args = self.parser.parse_args(['sync'])
        self.assertEqual(args.changelist, [])
        self.assertFalse(args.force)

    def test_sync_command_multiple_changelists(self):
        args = self.parser.parse_args(['sync', '123', '156', '178'])
        self.assertEqual(args.changelist, ['123', '156', '178'])

    def test_sync_command_with_force(self):
        args = self.parser.parse_args(['sync', '12345', '--force'])
        self.assertEqual(args.changelist, ['12345'])
        self.assertTrue(args.force)

    def test_sync_command_short_force(self):
        args = self.parser.parse_args(['sync', '100', '-f'])
        self.assertTrue(args.force)

    def test_sync_command_split_options(self):
        args = self.parser.parse_args(['sync'])
        self.assertIsNone(args.split_user)
        self.assertFalse(args.no_split)
        args = self.parser.parse_args(
            ['sync', '-u', 'alice', '--split-user', 'bob', '--no-split'])
        self.assertEqual(args.split_user, ['alice', 'bob'])
        self.assertTrue(args.no_split)

    def test_sync_command_dry_run(self):
        self.assertFalse(self.parser.parse_args(['sync']).dry_run)
        for flag in ('--dry-run', '-n'):
            args = self.parser.parse_args(['sync', flag])
            self.assertTrue(args.dry_run)

    def test_ignore_blocking_processes(self):
        self.assertFalse(self.parser.parse_args(
            ['sync']).ignore_blocking_processes)
        self.assertTrue(self.parser.parse_args(
            ['sync', '--ignore-blocking-processes'])
            .ignore_blocking_processes)

    def test_new_command_no_message(self):
        args = self.parser.parse_args(['new'])
        self.assertEqual(args.command, 'new')
        self.assertIsNone(args.message)

    def test_new_command(self):
        args = self.parser.parse_args(['new', '-m', 'Fix bug'])
        self.assertEqual(args.command, 'new')
        self.assertEqual(args.message, 'Fix bug')
        self.assertEqual(args.base_branch, 'HEAD~1')
        self.assertFalse(args.dry_run)
        self.assertFalse(args.no_edit)
        self.assertFalse(args.shelve)
        self.assertFalse(args.review)

    def test_message_file(self):
        for command in ('new', 'update', 'review'):
            args = self.parser.parse_args([command, '-F', 'msg.txt'])
            self.assertEqual(args.file, 'msg.txt')
            self.assertIsNone(args.message)

    def test_no_commit_list(self):
        for command in ('new', 'update', 'review'):
            self.assertFalse(
                self.parser.parse_args([command]).no_commit_list)
            self.assertTrue(self.parser.parse_args(
                [command, '--no-commit-list']).no_commit_list)

    def test_alias_delete_all(self):
        args = self.parser.parse_args(['alias', 'delete', '--all'])
        self.assertTrue(args.all)
        self.assertFalse(
            self.parser.parse_args(['alias', 'delete', 'x']).all)
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                self.parser.parse_args(['alias', 'delete', 'x', '--all'])

    def test_update_per_commit(self):
        self.assertFalse(self.parser.parse_args(['update']).per_commit)
        self.assertTrue(self.parser.parse_args(
            ['update', '--per-commit']).per_commit)

    def test_message_and_file_are_exclusive(self):
        for command in ('new', 'update', 'review'):
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.parser.parse_args(
                        [command, '-m', 'msg', '-F', 'msg.txt'])

    def test_new_command_with_options(self):
        args = self.parser.parse_args(
            ['new', '-m', 'Fix bug', '-b', 'main', '-n', '--no-edit',
             '--shelve', '--review'])
        self.assertEqual(args.message, 'Fix bug')
        self.assertEqual(args.base_branch, 'main')
        self.assertTrue(args.dry_run)
        self.assertTrue(args.no_edit)
        self.assertTrue(args.shelve)
        self.assertTrue(args.review)

    def test_new_command_with_alias(self):
        args = self.parser.parse_args(['new', '-m', 'Fix bug', 'myalias'])
        self.assertEqual(args.alias, 'myalias')

    def test_new_command_with_force(self):
        args = self.parser.parse_args(
            ['new', '-m', 'Fix bug', 'myalias', '-f'])
        self.assertTrue(args.force)

    def test_update_command(self):
        args = self.parser.parse_args(['update', '12345'])
        self.assertEqual(args.command, 'update')
        self.assertEqual(args.changelist, '12345')
        self.assertEqual(args.base_branch, 'HEAD~1')
        self.assertFalse(args.dry_run)
        self.assertFalse(args.no_edit)
        self.assertFalse(args.shelve)

    def test_update_command_with_options(self):
        args = self.parser.parse_args(
            ['update', '123', '-b', 'main', '-n', '--no-edit', '--shelve'])
        self.assertEqual(args.changelist, '123')
        self.assertEqual(args.base_branch, 'main')
        self.assertTrue(args.dry_run)
        self.assertTrue(args.no_edit)
        self.assertTrue(args.shelve)

    def test_update_defaults_to_branch(self):
        args = self.parser.parse_args(['update'])
        self.assertEqual(args.changelist, 'branch')

    def test_list_changes(self):
        args = self.parser.parse_args(['list-changes'])
        self.assertEqual(args.command, 'list-changes')
        self.assertEqual(args.base_branch, 'HEAD~1')

    def test_list_changes_with_base_branch(self):
        args = self.parser.parse_args(['list-changes', '-b', 'main'])
        self.assertEqual(args.base_branch, 'main')

    def test_sleep_option(self):
        args = self.parser.parse_args(['update', '100', '-s', '5'])
        self.assertEqual(args.sleep, 5)

    def test_invalid_sleep_rejected_at_parse_time(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                self.parser.parse_args(['update', '100', '-s', 'abc'])

    def test_sequence_editor_hidden_from_help(self):
        help_text = self.parser.format_help()
        self.assertNotIn('_sequence-editor', help_text)
        self.assertNotIn('SUPPRESS', help_text)

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            self.parser.parse_args(['--version'])
        self.assertEqual(ctx.exception.code, 0)


class TestHiddenSleep(unittest.TestCase):
    def test_sleep_is_parsed_but_not_listed_in_help(self):
        for command in ('new', 'update'):
            with self.subTest(command=command):
                args = create_parser().parse_args([command, '--sleep', '5'])
                self.assertEqual(args.sleep, 5)
                with contextlib.redirect_stdout(io.StringIO()) as out, \
                        self.assertRaises(SystemExit):
                    create_parser().parse_args([command, '-h'])
                self.assertNotIn('--sleep', out.getvalue())


@mock.patch('git_p4son.cli.get_workspace_dir', return_value='/ws')
class TestRunCommand(unittest.TestCase):
    @mock.patch('git_p4son.cli.sync_command', return_value=0)
    def test_dispatches_sync(self, mock_sync, _ws):
        parser = create_parser()
        args = parser.parse_args(['sync', '100'])
        result = run_command(args)
        mock_sync.assert_called_once_with(args)
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.sync_command', return_value=0)
    def test_dry_run_ends_with_one_final_line(self, _sync, _ws):
        with captured_output() as out:
            run_command(create_parser().parse_args(['sync', '-n']))
        self.assertTrue(out.getvalue().endswith(
            '\nDry run, nothing changed\n'))

    @mock.patch('git_p4son.cli.sync_command', return_value=0)
    def test_real_run_has_no_dry_run_line(self, _sync, _ws):
        with captured_output() as out:
            run_command(create_parser().parse_args(['sync']))
        self.assertNotIn('Dry run', out.getvalue())

    @mock.patch('git_p4son.cli.writable_command', return_value=0)
    def test_dispatches_writable(self, mock_writable, _ws):
        args = create_parser().parse_args(['writable', 'apply'])
        self.assertEqual(run_command(args), 0)
        mock_writable.assert_called_once_with(args)

    @mock.patch('git_p4son.cli.sync_split_users_command', return_value=0)
    def test_dispatches_sync_split_users(self, mock_split_users, _ws):
        args = create_parser().parse_args(['sync-split-users', 'list'])
        self.assertEqual(run_command(args), 0)
        mock_split_users.assert_called_once_with(args)

    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/x')
    @mock.patch('git_p4son.cli.new_command', return_value=0)
    def test_dispatches_new(self, mock_new, _branch, _ws):
        parser = create_parser()
        args = parser.parse_args(['new', '-m', 'msg'])
        result = run_command(args)
        mock_new.assert_called_once_with(args)
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.get_head_subject', return_value='Add feature')
    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/x')
    @mock.patch('git_p4son.cli.new_command', return_value=0)
    def test_new_defaults_message_from_head(self, mock_new, _branch, _head, _ws):
        parser = create_parser()
        args = parser.parse_args(['new'])
        result = run_command(args)
        self.assertEqual(args.message, 'Add feature')
        mock_new.assert_called_once_with(args)
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.get_head_subject', return_value='Add feature')
    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/x')
    @mock.patch('git_p4son.cli.new_command', return_value=0)
    def test_new_explicit_message_takes_precedence(self, mock_new, _branch, _head, _ws):
        parser = create_parser()
        args = parser.parse_args(['new', '-m', 'Explicit msg'])
        result = run_command(args)
        self.assertEqual(args.message, 'Explicit msg')
        _head.assert_not_called()
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.get_head_subject', return_value=None)
    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/x')
    def test_new_no_commits_returns_error(self, _branch, _head, _ws):
        parser = create_parser()
        args = parser.parse_args(['new'])
        with self.assertRaisesRegex(CommandError, 'No commits found'):
            run_command(args)

    @mock.patch('git_p4son.cli.get_head_subject', return_value='Review change')
    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/bar')
    @mock.patch('git_p4son.cli.review_command', return_value=0)
    def test_review_defaults_message_from_head(self, mock_review, _branch, _head, _ws):
        parser = create_parser()
        args = parser.parse_args(['review'])
        result = run_command(args)
        self.assertEqual(args.message, 'Review change')
        mock_review.assert_called_once_with(args)
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.get_head_subject', return_value='Add feature')
    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/x')
    @mock.patch('git_p4son.cli.update_command', return_value=0)
    def test_update_keeps_message_unset(self, mock_update, _branch, _head, _ws):
        args = create_parser().parse_args(['update'])
        result = run_command(args)
        self.assertIsNone(args.message)
        _head.assert_not_called()
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.get_head_subject', return_value='Add feature')
    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/x')
    @mock.patch('git_p4son.cli.new_command', return_value=0)
    def test_message_read_from_file(self, mock_new, _branch, _head, _ws):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'msg.txt'), 'w') as f:
                f.write('Title\n\nBody line\n\n')
            args = create_parser().parse_args(['new', '-F', 'msg.txt'])
            with mock.patch('os.getcwd', return_value=tmp):
                result = run_command(args)
        self.assertEqual(result, 0)
        self.assertEqual(args.message, 'Title\n\nBody line')
        self.assertEqual(args.file, os.path.join(tmp, 'msg.txt'))
        _head.assert_not_called()

    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/x')
    @mock.patch('git_p4son.cli.new_command', return_value=0)
    def test_missing_message_file_returns_error(self, mock_new, _branch, _ws):
        with tempfile.TemporaryDirectory() as tmp:
            args = create_parser().parse_args(['new', '-F', 'missing.txt'])
            with mock.patch('os.getcwd', return_value=tmp), \
                    self.assertRaisesRegex(CommandError,
                                           'Failed to read message file'):
                run_command(args)
        mock_new.assert_not_called()

    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/x')
    @mock.patch('git_p4son.cli.new_command', return_value=0)
    def test_empty_message_file_returns_error(self, mock_new, _branch, _ws):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, 'msg.txt'), 'w') as f:
                f.write('\n  \n')
            args = create_parser().parse_args(['new', '-F', 'msg.txt'])
            with mock.patch('os.getcwd', return_value=tmp), \
                    self.assertRaisesRegex(CommandError, 'is empty'):
                run_command(args)
        mock_new.assert_not_called()

    @mock.patch('git_p4son.cli.update_command', return_value=0)
    def test_dispatches_update(self, mock_update, _ws):
        parser = create_parser()
        args = parser.parse_args(['update', '100'])
        result = run_command(args)
        mock_update.assert_called_once_with(args)
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.list_changes_command', return_value=0)
    def test_dispatches_list_changes(self, mock_lc, _ws):
        parser = create_parser()
        args = parser.parse_args(['list-changes'])
        result = run_command(args)
        mock_lc.assert_called_once_with(args)
        self.assertEqual(result, 0)


class TestResolveBranchKeyword(unittest.TestCase):
    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/foo')
    def test_resolves_branch_to_alias(self, _branch):
        result = _resolve_branch_keyword('/ws')
        self.assertEqual(result, 'feat-foo')

    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/bar')
    @mock.patch('git_p4son.cli.get_workspace_dir', return_value='/ws')
    @mock.patch('git_p4son.cli.review_command', return_value=0)
    def test_run_command_resolves_at_branch_for_review(
            self, mock_review, _ws, _branch):
        args = create_parser().parse_args(['review', '-m', 'msg'])
        result = run_command(args)
        self.assertEqual(args.alias, 'feat-bar')
        mock_review.assert_called_once_with(args)
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/bar')
    @mock.patch('git_p4son.cli.get_workspace_dir', return_value='/ws')
    @mock.patch('git_p4son.alias.load_changelist_alias', return_value='100')
    def test_alias_show_prints_only_the_changelist(
            self, mock_load, _ws, _branch):
        """Resolves the branch and keeps all logging off stdout."""
        args = create_parser().parse_args(['alias', 'show'])
        with mock.patch.object(log, 'quiet_mode', False):
            with contextlib.redirect_stdout(io.StringIO()) as out:
                result = run_command(args)
        self.assertEqual(result, 0)
        self.assertEqual(out.getvalue(), '100\n')
        mock_load.assert_called_once_with('feat-bar', '/ws')

    @mock.patch('git_p4son.cli.get_workspace_dir', return_value='/ws')
    def test_alias_without_action_asks_for_one(self, _ws):
        args = create_parser().parse_args(['alias'])
        with self.assertRaisesRegex(
                CommandError,
                '^No alias action specified. Use "git-p4son alias -h" for help.$'):
            run_command(args)

    @mock.patch('git_p4son.cli.get_current_branch', return_value=None)
    @mock.patch('git_p4son.cli.get_workspace_dir', return_value='/ws')
    @mock.patch('git_p4son.cli.alias_delete_command', return_value=0)
    def test_alias_delete_all_skips_resolution(
            self, mock_alias, _ws, mock_branch):
        """--all names no alias, so it works on a detached HEAD."""
        args = create_parser().parse_args(['alias', 'delete', '--all'])
        self.assertEqual(run_command(args), 0)
        mock_branch.assert_not_called()
        mock_alias.assert_called_once_with(args)

    @mock.patch('git_p4son.cli.get_current_branch', return_value=None)
    def test_detached_head_raises(self, _branch):
        with self.assertRaisesRegex(CommandError, 'detached HEAD'):
            _resolve_branch_keyword('/ws')

    @mock.patch('git_p4son.cli.get_current_branch', return_value='main')
    def test_main_branch_resolves_to_main(self, _branch):
        result = _resolve_branch_keyword('/ws')
        self.assertEqual(result, 'main')

    def test_new_defaults_to_branch(self):
        args = create_parser().parse_args(['new', '-m', 'msg'])
        self.assertEqual(args.alias, 'branch')

    @mock.patch('git_p4son.cli.new_command', return_value=0)
    @mock.patch('git_p4son.cli.get_workspace_dir', return_value='/ws')
    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/xyz')
    def test_no_alias_skips_resolution(self, _branch, _ws, mock_new):
        args = create_parser().parse_args(['new', '-m', 'msg', '--no-alias'])
        result = run_command(args)
        self.assertIsNone(args.alias)
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.new_command', return_value=0)
    @mock.patch('git_p4son.cli.get_workspace_dir', return_value='/ws')
    def test_explicit_alias_unchanged(self, _ws, mock_new):
        args = create_parser().parse_args(['new', '-m', 'msg', 'myalias'])
        result = run_command(args)
        self.assertEqual(args.alias, 'myalias')
        self.assertEqual(result, 0)


class TestCommandsWithoutWorkspace(unittest.TestCase):
    """init, completion and skill run outside a git workspace."""

    @mock.patch('git_p4son.cli.get_workspace_dir')
    @mock.patch('git_p4son.cli.init_command', return_value=0)
    def test_init(self, mock_init, mock_ws):
        args = create_parser().parse_args(['init'])
        self.assertEqual(run_command(args), 0)
        mock_init.assert_called_once_with(args)
        mock_ws.assert_not_called()

    @mock.patch('git_p4son.cli.get_workspace_dir')
    @mock.patch('git_p4son.cli.completion_command', return_value=0)
    def test_completion(self, mock_completion, mock_ws):
        args = create_parser().parse_args(['completion', 'zsh'])
        self.assertEqual(run_command(args), 0)
        mock_completion.assert_called_once_with(args)
        mock_ws.assert_not_called()

    @mock.patch('git_p4son.cli.get_workspace_dir')
    @mock.patch('git_p4son.cli.skill_show_command', return_value=0)
    def test_skill(self, mock_show, mock_ws):
        args = create_parser().parse_args(['skill', 'show'])
        self.assertEqual(run_command(args), 0)
        mock_show.assert_called_once_with(args)
        mock_ws.assert_not_called()

    @mock.patch('git_p4son.cli.get_workspace_dir')
    def test_skill_without_action_asks_for_one(self, mock_ws):
        args = create_parser().parse_args(['skill'])
        with self.assertRaisesRegex(
                CommandError,
                '^No skill action specified. Use "git-p4son skill -h" for help.$'):
            run_command(args)
        mock_ws.assert_not_called()


@mock.patch('sys.argv', ['git-p4son', 'list-changes'])
class TestMain(unittest.TestCase):
    """main turns what run_command raises into error lines and an exit code."""

    def _main(self, side_effect):
        with mock.patch('git_p4son.cli.run_command', side_effect=side_effect), \
                contextlib.redirect_stderr(io.StringIO()) as err, \
                contextlib.redirect_stdout(io.StringIO()):
            rc = main()
        return rc, err.getvalue()

    def test_success_returns_the_command_exit_code(self):
        self.assertEqual(self._main([0])[0], 0)

    def test_run_error_prints_stderr_and_passes_on_the_return_code(self):
        rc, err = self._main(RunError('p4 sync', returncode=4,
                                      stderr=['p4: boom']))
        self.assertEqual(rc, 4)
        self.assertIn('p4: boom', err)
        self.assertIn('Process failed with return code 4', err)

    def test_command_error_prints_its_message_and_return_code(self):
        rc, err = self._main(CommandError('Nope', returncode=3))
        self.assertEqual(rc, 3)
        self.assertIn('Nope', err)

    def test_ctrl_c_is_reported_as_cancelled(self):
        rc, err = self._main(KeyboardInterrupt())
        self.assertEqual(rc, 1)
        self.assertIn('Operation cancelled by user', err)

    def test_unexpected_error_prints_its_message(self):
        rc, err = self._main(ValueError('weird'))
        self.assertEqual(rc, 1)
        self.assertIn('weird', err)


if __name__ == '__main__':
    unittest.main()
