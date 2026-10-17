"""Tests for git_p4son.alias module."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from git_p4son.alias import (
    alias_clean_command,
    alias_delete_command,
    alias_list_command,
    alias_new_command,
    alias_show_command,
)
from git_p4son.common import CommandError
from tests.helpers import captured_output, cli_args, WorkspaceTestCase


ALIASES = [('feature-a', '100'), ('feature-b', '200'), ('feature-c', '300')]


class TestAliasShowCommand(unittest.TestCase):
    @mock.patch('git_p4son.alias.load_changelist_alias', return_value='100')
    def test_prints_only_the_changelist(self, mock_load):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = alias_show_command(
                cli_args(['alias', 'show', 'feature-a']))
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), '100\n')
        mock_load.assert_called_once_with('feature-a', '/ws')

    def test_missing_alias_fails(self):
        with tempfile.TemporaryDirectory() as ws, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaisesRegex(CommandError, 'No changelist alias found'):
                alias_show_command(
                    cli_args(['alias', 'show', 'nope'], workspace_dir=ws))
        self.assertEqual(out.getvalue(), '')


@mock.patch('git_p4son.alias.delete_changelist_alias', return_value=True)
@mock.patch('git_p4son.alias.list_changelist_aliases', return_value=ALIASES)
class TestAliasDeleteCommand(unittest.TestCase):
    def test_deletes_named_alias(self, mock_list, mock_delete):
        rc = alias_delete_command(
            cli_args(['alias', 'delete', 'feature-b']))
        self.assertEqual(rc, 0)
        mock_delete.assert_called_once_with('feature-b', '/ws')
        mock_list.assert_not_called()

    def test_all_deletes_everything_without_prompting(
            self, _list, mock_delete):
        with mock.patch('builtins.input') as mock_input:
            rc = alias_delete_command(
                cli_args(['alias', 'delete', '--all']))
        self.assertEqual(rc, 0)
        mock_input.assert_not_called()
        self.assertEqual(
            [c.args[0] for c in mock_delete.call_args_list],
            ['feature-a', 'feature-b', 'feature-c'])

    def test_all_goes_on_past_a_failed_delete(self, _list, mock_delete):
        mock_delete.side_effect = [None, CommandError('boom'), None]
        with captured_output() as out:
            rc = alias_delete_command(
                cli_args(['alias', 'delete', '--all']))
        self.assertEqual(rc, 0)
        self.assertEqual(mock_delete.call_count, 3)
        self.assertIn('[err] boom\n', out.getvalue())

    def test_all_without_aliases(self, mock_list, mock_delete):
        mock_list.return_value = []
        rc = alias_delete_command(
            cli_args(['alias', 'delete', '--all']))
        self.assertEqual(rc, 0)
        mock_delete.assert_not_called()


@mock.patch('git_p4son.alias.delete_changelist_alias', return_value=True)
@mock.patch('git_p4son.alias.list_changelist_aliases', return_value=ALIASES)
class TestAliasCleanCommand(unittest.TestCase):
    def test_no_aliases_does_not_prompt(self, mock_list, mock_delete):
        mock_list.return_value = []
        with mock.patch('builtins.input') as mock_input:
            rc = alias_clean_command(cli_args(['alias', 'clean']))
        self.assertEqual(rc, 0)
        mock_input.assert_not_called()
        mock_delete.assert_not_called()

    def test_quit_deletes_nothing(self, _list, mock_delete):
        with mock.patch('builtins.input', return_value='q'):
            rc = alias_clean_command(cli_args(['alias', 'clean']))
        self.assertEqual(rc, 0)
        mock_delete.assert_not_called()

    def test_eof_deletes_nothing(self, _list, mock_delete):
        with mock.patch('builtins.input', side_effect=EOFError):
            rc = alias_clean_command(cli_args(['alias', 'clean']))
        self.assertEqual(rc, 0)
        mock_delete.assert_not_called()

    def test_all_deletes_everything_without_more_prompts(
            self, _list, mock_delete):
        with mock.patch('builtins.input', return_value='a') as mock_input:
            rc = alias_clean_command(cli_args(['alias', 'clean']))
        self.assertEqual(rc, 0)
        self.assertEqual(mock_input.call_count, 1)
        self.assertEqual(
            mock_delete.call_args_list,
            [mock.call('feature-a', '/ws'),
             mock.call('feature-b', '/ws'),
             mock.call('feature-c', '/ws')])

    def test_full_words_accepted(self, _list, mock_delete):
        with mock.patch('builtins.input', return_value='all'):
            rc = alias_clean_command(cli_args(['alias', 'clean']))
        self.assertEqual(rc, 0)
        self.assertEqual(mock_delete.call_count, 3)

    def test_invalid_mode_reprompts(self, _list, mock_delete):
        with mock.patch('builtins.input',
                        side_effect=['x', '', 'quit']) as mock_input:
            rc = alias_clean_command(cli_args(['alias', 'clean']))
        self.assertEqual(rc, 0)
        self.assertEqual(mock_input.call_count, 3)
        mock_delete.assert_not_called()

    def test_interactive_yes_no_quit(self, _list, mock_delete):
        with mock.patch('builtins.input',
                        side_effect=['i', 'y', 'n', 'q']):
            rc = alias_clean_command(cli_args(['alias', 'clean']))
        self.assertEqual(rc, 0)
        mock_delete.assert_called_once_with('feature-a', '/ws')

    def test_interactive_all_deletes_remaining(self, _list, mock_delete):
        """Answering no to the aliases worth keeping, then all, sweeps the rest without prompts."""
        with mock.patch('builtins.input',
                        side_effect=['i', 'n', 'a']) as mock_input:
            rc = alias_clean_command(cli_args(['alias', 'clean']))
        self.assertEqual(rc, 0)
        self.assertEqual(mock_input.call_count, 3)
        self.assertEqual(
            mock_delete.call_args_list,
            [mock.call('feature-b', '/ws'),
             mock.call('feature-c', '/ws')])

    def test_interactive_eof_aborts(self, _list, mock_delete):
        with mock.patch('builtins.input', side_effect=['i', 'y', EOFError]):
            rc = alias_clean_command(cli_args(['alias', 'clean']))
        self.assertEqual(rc, 0)
        mock_delete.assert_called_once_with('feature-a', '/ws')


class TestAliasNewAndList(WorkspaceTestCase):
    """alias new and alias list against a real alias store."""

    def _new(self, changelist, alias, force=False):
        return alias_new_command(cli_args(
            ['alias', 'new', changelist, alias] + (['-f'] if force else []),
            workspace_dir=self.ws))

    def _list(self):
        with captured_output() as out:
            alias_list_command(
                cli_args(['alias', 'list'], workspace_dir=self.ws))
        return out.getvalue().splitlines()

    def test_new_aliases_are_listed_in_name_order(self):
        self.assertEqual(self._new('200', 'feature-b'), 0)
        self.assertEqual(self._new('100', 'feature-a'), 0)
        self.assertEqual(self._list(), ['Found 2 aliases',
                                        'feature-a -> CL 100',
                                        'feature-b -> CL 200'])

    def test_no_aliases(self):
        self.assertEqual(self._list(), ['No aliases defined'])

    def test_empty_alias_files_are_left_out(self):
        self._new('100', 'feature-a')
        open(os.path.join(self.ws, '.git-p4son', 'aliases', 'empty.toml'),
             'w').close()
        os.makedirs(os.path.join(self.ws, '.git-p4son', 'changelists'))
        open(os.path.join(self.ws, '.git-p4son', 'changelists', 'old-empty'),
             'w').close()
        self.assertEqual(self._list(), ['Found 1 alias',
                                        'feature-a -> CL 100'])

    def test_new_refuses_a_non_numeric_changelist(self):
        with self.assertRaisesRegex(CommandError,
                                    'Invalid changelist number: abc'):
            self._new('abc', 'feature-a')
        self.assertEqual(self._list(), ['No aliases defined'])


class TestAliasCleanMigrates(WorkspaceTestCase):
    """alias clean against a real alias store with aliases in the old one."""

    def setUp(self):
        super().setUp()
        self.old_dir = os.path.join(self.ws, '.git-p4son', 'changelists')
        os.makedirs(self.old_dir)
        for name, changelist in (('feature-a', '100'), ('feature-b', '200')):
            with open(os.path.join(self.old_dir, name), 'w') as f:
                f.write(changelist + '\n')

    def _clean(self, answers):
        with mock.patch('builtins.input', side_effect=answers), \
                captured_output() as out:
            rc = alias_clean_command(
                cli_args(['alias', 'clean'], workspace_dir=self.ws))
        self.assertEqual(rc, 0)
        return out.getvalue()

    def _new_store(self):
        return sorted(os.listdir(
            os.path.join(self.ws, '.git-p4son', 'aliases')))

    def test_clean_moves_the_old_aliases_before_asking(self):
        out = self._clean(['q'])
        self.assertIn('Moved 2 aliases to .git-p4son/aliases/', out)
        self.assertLess(out.index('Moved 2 aliases'),
                        out.index('Found 2 aliases'))
        self.assertFalse(os.path.exists(self.old_dir))
        self.assertEqual(self._new_store(),
                         ['feature-a.toml', 'feature-b.toml'])

    def test_clean_deletes_from_the_new_store(self):
        self._clean(['i', 'y', 'n'])
        self.assertEqual(self._new_store(), ['feature-b.toml'])


if __name__ == '__main__':
    unittest.main()
