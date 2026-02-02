"""Tests for git_p4son.cli module."""

import unittest
from unittest import mock

from git_p4son.cli import create_parser, run_command


class TestCreateParser(unittest.TestCase):
    def setUp(self):
        self.parser = create_parser()

    def test_sync_command_basic(self):
        args = self.parser.parse_args(['sync', '12345'])
        self.assertEqual(args.command, 'sync')
        self.assertEqual(args.changelist, '12345')
        self.assertFalse(args.force)

    def test_sync_command_with_force(self):
        args = self.parser.parse_args(['sync', 'latest', '--force'])
        self.assertEqual(args.changelist, 'latest')
        self.assertTrue(args.force)

    def test_sync_command_short_force(self):
        args = self.parser.parse_args(['sync', '100', '-f'])
        self.assertTrue(args.force)

    def test_edit_command(self):
        args = self.parser.parse_args(['edit', '12345'])
        self.assertEqual(args.command, 'edit')
        self.assertEqual(args.changelist, '12345')
        self.assertEqual(args.base_branch, 'HEAD~1')
        self.assertFalse(args.dry_run)

    def test_edit_command_with_options(self):
        args = self.parser.parse_args(['edit', '99', '-b', 'main', '-n'])
        self.assertEqual(args.base_branch, 'main')
        self.assertTrue(args.dry_run)

    def test_changelist_new(self):
        args = self.parser.parse_args(['changelist', 'new', '-m', 'Fix bug'])
        self.assertEqual(args.command, 'changelist')
        self.assertEqual(args.changelist_action, 'new')
        self.assertEqual(args.message, 'Fix bug')
        self.assertEqual(args.base_branch, 'HEAD~1')

    def test_changelist_update(self):
        args = self.parser.parse_args(
            ['changelist', 'update', '123', '-b', 'main'])
        self.assertEqual(args.changelist_action, 'update')
        self.assertEqual(args.changelist, '123')
        self.assertEqual(args.base_branch, 'main')

    def test_list_changes(self):
        args = self.parser.parse_args(['list-changes'])
        self.assertEqual(args.command, 'list-changes')
        self.assertEqual(args.base_branch, 'HEAD~1')

    def test_list_changes_with_base_branch(self):
        args = self.parser.parse_args(['list-changes', '-b', 'main'])
        self.assertEqual(args.base_branch, 'main')

    def test_review_new(self):
        args = self.parser.parse_args(['review', 'new', '-m', 'Review this'])
        self.assertEqual(args.command, 'review')
        self.assertEqual(args.review_action, 'new')
        self.assertEqual(args.message, 'Review this')

    def test_review_update(self):
        args = self.parser.parse_args(['review', 'update', '456'])
        self.assertEqual(args.review_action, 'update')
        self.assertEqual(args.changelist, '456')
        self.assertFalse(args.description)

    def test_review_update_with_description(self):
        args = self.parser.parse_args(['review', 'update', '456', '-d'])
        self.assertTrue(args.description)

    def test_sleep_option(self):
        args = self.parser.parse_args(['-s', '5', 'sync', '100'])
        self.assertEqual(args.sleep, '5')

    def test_version_flag(self):
        with self.assertRaises(SystemExit) as ctx:
            self.parser.parse_args(['--version'])
        self.assertEqual(ctx.exception.code, 0)


class TestRunCommand(unittest.TestCase):
    @mock.patch('git_p4son.cli.sync_command', return_value=0)
    def test_dispatches_sync(self, mock_sync):
        parser = create_parser()
        args = parser.parse_args(['sync', '100'])
        result = run_command(args)
        mock_sync.assert_called_once_with(args)
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.edit_command', return_value=0)
    def test_dispatches_edit(self, mock_edit):
        parser = create_parser()
        args = parser.parse_args(['edit', '100'])
        result = run_command(args)
        mock_edit.assert_called_once_with(args)
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.changelist_command', return_value=0)
    def test_dispatches_changelist(self, mock_cl):
        parser = create_parser()
        args = parser.parse_args(['changelist', 'new', '-m', 'msg'])
        result = run_command(args)
        mock_cl.assert_called_once_with(args)
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.list_changes_command', return_value=0)
    def test_dispatches_list_changes(self, mock_lc):
        parser = create_parser()
        args = parser.parse_args(['list-changes'])
        result = run_command(args)
        mock_lc.assert_called_once_with(args)
        self.assertEqual(result, 0)

    @mock.patch('git_p4son.cli.review_command', return_value=0)
    def test_dispatches_review(self, mock_review):
        parser = create_parser()
        args = parser.parse_args(['review', 'new', '-m', 'msg'])
        result = run_command(args)
        mock_review.assert_called_once_with(args)
        self.assertEqual(result, 0)


if __name__ == '__main__':
    unittest.main()
