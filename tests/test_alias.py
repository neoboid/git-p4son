"""Tests for git_p4son.alias module."""

import unittest
from unittest import mock

from git_p4son.alias import _prompt_choice, alias_clean_command


class TestPromptChoice(unittest.TestCase):
    def test_renders_prompt_from_options(self):
        with mock.patch('builtins.input', return_value='n') as mock_input:
            result = _prompt_choice('Delete?', ['yes', 'no'])
        self.assertEqual(result, 'no')
        mock_input.assert_called_once_with('Delete? [y]es / [n]o: ')

    def test_accepts_full_word(self):
        with mock.patch('builtins.input', return_value='quit'):
            self.assertEqual(
                _prompt_choice('Delete', ['all', 'quit']), 'quit')


def _args(workspace_dir='/ws'):
    return mock.Mock(workspace_dir=workspace_dir)


ALIASES = [('feature-a', '100'), ('feature-b', '200'), ('feature-c', '300')]


@mock.patch('git_p4son.alias.delete_changelist_alias', return_value=True)
@mock.patch('git_p4son.alias.list_changelist_aliases', return_value=ALIASES)
class TestAliasCleanCommand(unittest.TestCase):
    def test_no_aliases_does_not_prompt(self, mock_list, mock_delete):
        mock_list.return_value = []
        with mock.patch('builtins.input') as mock_input:
            rc = alias_clean_command(_args())
        self.assertEqual(rc, 0)
        mock_input.assert_not_called()
        mock_delete.assert_not_called()

    def test_quit_deletes_nothing(self, _list, mock_delete):
        with mock.patch('builtins.input', return_value='q'):
            rc = alias_clean_command(_args())
        self.assertEqual(rc, 0)
        mock_delete.assert_not_called()

    def test_eof_deletes_nothing(self, _list, mock_delete):
        with mock.patch('builtins.input', side_effect=EOFError):
            rc = alias_clean_command(_args())
        self.assertEqual(rc, 0)
        mock_delete.assert_not_called()

    def test_all_deletes_everything_without_more_prompts(
            self, _list, mock_delete):
        with mock.patch('builtins.input', return_value='a') as mock_input:
            rc = alias_clean_command(_args())
        self.assertEqual(rc, 0)
        self.assertEqual(mock_input.call_count, 1)
        self.assertEqual(
            mock_delete.call_args_list,
            [mock.call('feature-a', '/ws'),
             mock.call('feature-b', '/ws'),
             mock.call('feature-c', '/ws')])

    def test_full_words_accepted(self, _list, mock_delete):
        with mock.patch('builtins.input', return_value='all'):
            rc = alias_clean_command(_args())
        self.assertEqual(rc, 0)
        self.assertEqual(mock_delete.call_count, 3)

    def test_invalid_mode_reprompts(self, _list, mock_delete):
        with mock.patch('builtins.input',
                        side_effect=['x', '', 'quit']) as mock_input:
            rc = alias_clean_command(_args())
        self.assertEqual(rc, 0)
        self.assertEqual(mock_input.call_count, 3)
        mock_delete.assert_not_called()

    def test_interactive_yes_no_quit(self, _list, mock_delete):
        with mock.patch('builtins.input',
                        side_effect=['i', 'y', 'n', 'q']):
            rc = alias_clean_command(_args())
        self.assertEqual(rc, 0)
        mock_delete.assert_called_once_with('feature-a', '/ws')

    def test_interactive_all_deletes_remaining(self, _list, mock_delete):
        """Answering no through the aliases worth keeping and then all
        sweeps the remainder without further prompts."""
        with mock.patch('builtins.input',
                        side_effect=['i', 'n', 'a']) as mock_input:
            rc = alias_clean_command(_args())
        self.assertEqual(rc, 0)
        self.assertEqual(mock_input.call_count, 3)
        self.assertEqual(
            mock_delete.call_args_list,
            [mock.call('feature-b', '/ws'),
             mock.call('feature-c', '/ws')])

    def test_interactive_eof_aborts(self, _list, mock_delete):
        with mock.patch('builtins.input', side_effect=['i', 'y', EOFError]):
            rc = alias_clean_command(_args())
        self.assertEqual(rc, 0)
        mock_delete.assert_called_once_with('feature-a', '/ws')


if __name__ == '__main__':
    unittest.main()
