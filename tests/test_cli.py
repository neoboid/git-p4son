"""Tests for git_p4son.cli module."""

import unittest
from unittest import mock

from git_p4son.cli import create_parser, run_command, _resolve_branch_keyword


class TestCreateParser(unittest.TestCase):
    def setUp(self):
        self.parser = create_parser()

    def test_sync_command_basic(self):
        args = self.parser.parse_args(['sync', '12345'])
        self.assertEqual(args.command, 'sync')
        self.assertEqual(args.changelist, '12345')
        self.assertFalse(args.force)

    def test_sync_command_no_changelist(self):
        args = self.parser.parse_args(['sync'])
        self.assertIsNone(args.changelist)
        self.assertFalse(args.force)

    def test_sync_command_with_force(self):
        args = self.parser.parse_args(['sync', '12345', '--force'])
        self.assertEqual(args.changelist, '12345')
        self.assertTrue(args.force)

    def test_sync_command_short_force(self):
        args = self.parser.parse_args(['sync', '100', '-f'])
        self.assertTrue(args.force)

    def test_new_command(self):
        args = self.parser.parse_args(['new', '-m', 'Fix bug'])
        self.assertEqual(args.command, 'new')
        self.assertEqual(args.message, 'Fix bug')
        self.assertEqual(args.base_branch, 'HEAD~1')
        self.assertFalse(args.dry_run)
        self.assertFalse(args.no_edit)
        self.assertFalse(args.shelve)
        self.assertFalse(args.review)

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
        self.assertEqual(args.sleep, '5')

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            self.parser.parse_args(['--version'])
        self.assertEqual(ctx.exception.code, 0)


@mock.patch('git_p4son.cli.get_workspace_dir', return_value='/ws')
class TestRunCommand(unittest.TestCase):
    @mock.patch('git_p4son.cli.sync_command', return_value=0)
    def test_dispatches_sync(self, mock_sync, _ws):
        parser = create_parser()
        args = parser.parse_args(['sync', '100'])
        result = run_command(args)
        mock_sync.assert_called_once_with(args)
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/x')
    @mock.patch('git_p4son.cli.new_command', return_value=0)
    def test_dispatches_new(self, mock_new, _branch, _ws):
        parser = create_parser()
        args = parser.parse_args(['new', '-m', 'msg'])
        result = run_command(args)
        mock_new.assert_called_once_with(args)
        self.assertEqual(result, 0)

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
    def setUp(self):
        self.parser = create_parser()

    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/foo')
    def test_resolves_branch_to_alias(self, _branch):
        result = _resolve_branch_keyword('/ws')
        self.assertEqual(result, 'feat-foo')

    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/bar')
    @mock.patch('git_p4son.cli.get_workspace_dir', return_value='/ws')
    @mock.patch('git_p4son.cli.review_command', return_value=0)
    def test_run_command_resolves_at_branch_for_review(
            self, mock_review, _ws, _branch):
        args = self.parser.parse_args(['review', '-m', 'msg'])
        result = run_command(args)
        self.assertEqual(args.alias, 'feat-bar')
        mock_review.assert_called_once_with(args)
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.get_current_branch', return_value=None)
    def test_detached_head_returns_none(self, _branch):
        result = _resolve_branch_keyword('/ws')
        self.assertIsNone(result)

    @mock.patch('git_p4son.cli.get_current_branch', return_value='main')
    def test_main_branch_resolves_to_main(self, _branch):
        result = _resolve_branch_keyword('/ws')
        self.assertEqual(result, 'main')

    def test_new_defaults_to_branch(self):
        args = self.parser.parse_args(['new', '-m', 'msg'])
        self.assertEqual(args.alias, 'branch')

    @mock.patch('git_p4son.cli.new_command', return_value=0)
    @mock.patch('git_p4son.cli.get_workspace_dir', return_value='/ws')
    @mock.patch('git_p4son.cli.get_current_branch', return_value='feat/xyz')
    def test_no_alias_skips_resolution(self, _branch, _ws, mock_new):
        args = self.parser.parse_args(['new', '-m', 'msg', '--no-alias'])
        result = run_command(args)
        self.assertIsNone(args.alias)
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.new_command', return_value=0)
    @mock.patch('git_p4son.cli.get_workspace_dir', return_value='/ws')
    def test_explicit_alias_unchanged(self, _ws, mock_new):
        args = self.parser.parse_args(['new', '-m', 'msg', 'myalias'])
        result = run_command(args)
        self.assertEqual(args.alias, 'myalias')
        self.assertEqual(result, 0)


if __name__ == '__main__':
    unittest.main()
