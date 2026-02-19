"""Tests for git_p4son.complete module."""

import argparse
import unittest
from unittest import mock

from git_p4son.cli import create_parser
from git_p4son.complete import (
    _complete,
    _filter,
    _flag_takes_value,
    _get_flags,
)


class TestGetFlags(unittest.TestCase):
    def test_extracts_flags_from_parser(self):
        parser = create_parser()
        flags = _get_flags(parser)
        flag_names = [name for name, _ in flags]
        self.assertIn('--version', flag_names)

    def test_excludes_help_action(self):
        parser = create_parser()
        flags = _get_flags(parser)
        flag_names = [name for name, _ in flags]
        self.assertNotIn('-h', flag_names)
        self.assertNotIn('--help', flag_names)

    def test_excludes_suppressed_flags(self):
        parser = argparse.ArgumentParser()
        parser.add_argument('--hidden', help=argparse.SUPPRESS)
        parser.add_argument('--visible', help='A visible flag')
        flags = _get_flags(parser)
        flag_names = [name for name, _ in flags]
        self.assertNotIn('--hidden', flag_names)
        self.assertIn('--visible', flag_names)

    def test_version_action_included(self):
        parser = create_parser()
        flags = _get_flags(parser)
        flag_names = [name for name, _ in flags]
        self.assertIn('--version', flag_names)


class TestFlagTakesValue(unittest.TestCase):
    def test_store_true_returns_false(self):
        parser = argparse.ArgumentParser()
        parser.add_argument('--flag', action='store_true')
        action = parser._actions[-1]
        self.assertFalse(_flag_takes_value(action))

    def test_store_returns_true(self):
        parser = argparse.ArgumentParser()
        parser.add_argument('--message', type=str)
        action = parser._actions[-1]
        self.assertTrue(_flag_takes_value(action))


class TestFilter(unittest.TestCase):
    def test_filters_by_prefix(self):
        candidates = [('alpha', 'a'), ('beta', 'b'), ('able', 'c')]
        result = _filter(candidates, 'a')
        self.assertEqual(result, [('alpha', 'a'), ('able', 'c')])

    def test_empty_prefix_returns_all(self):
        candidates = [('x', '1'), ('y', '2')]
        result = _filter(candidates, '')
        self.assertEqual(result, [('x', '1'), ('y', '2')])

    def test_no_match_returns_empty(self):
        candidates = [('alpha', 'a')]
        result = _filter(candidates, 'z')
        self.assertEqual(result, [])


@mock.patch('git_p4son.complete.list_changelist_aliases',
            return_value=[('myalias', '999')])
@mock.patch('git_p4son.complete.get_workspace_dir', return_value='/ws')
class TestComplete(unittest.TestCase):
    def setUp(self):
        self.parser = create_parser()

    def _names(self, candidates):
        return [name for name, _ in candidates]

    # -- Command completion --

    def test_empty_lists_all_commands(self, _ws, _aliases):
        result = _complete(self.parser, [''], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('sync', names)
        self.assertIn('new', names)
        self.assertIn('update', names)
        self.assertIn('list-changes', names)
        self.assertIn('alias', names)
        self.assertIn('review', names)
        self.assertEqual(len(names), 6)

    def test_empty_excludes_complete(self, _ws, _aliases):
        result = _complete(self.parser, [''], workspace_dir='/ws')
        names = self._names(result)
        self.assertNotIn('complete', names)

    def test_hidden_sequence_editor_not_completed(self, _ws, _aliases):
        result = _complete(self.parser, [''], workspace_dir='/ws')
        names = self._names(result)
        self.assertNotIn('_sequence-editor', names)

    def test_prefix_filters_commands(self, _ws, _aliases):
        result = _complete(self.parser, ['sy'], workspace_dir='/ws')
        names = self._names(result)
        self.assertEqual(names, ['sync'])

    def test_hidden_command_not_completed(self, _ws, _aliases):
        result = _complete(self.parser, ['c'], workspace_dir='/ws')
        names = self._names(result)
        self.assertEqual(names, [])

    # -- Global flags --

    def test_global_flag_completion(self, _ws, _aliases):
        result = _complete(self.parser, ['--'], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('--version', names)

    # -- sync command --

    def test_sync_positional(self, _ws, _aliases):
        result = _complete(self.parser, ['sync', ''], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('latest', names)
        self.assertIn('last-synced', names)
        self.assertIn('myalias', names)

    def test_sync_positional_prefix(self, _ws, _aliases):
        result = _complete(self.parser, ['sync', 'la'], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('latest', names)
        self.assertIn('last-synced', names)
        self.assertNotIn('myalias', names)

    def test_sync_flags(self, _ws, _aliases):
        result = _complete(self.parser, ['sync', '-'], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('-f', names)
        self.assertIn('--force', names)

    # -- new command --

    def test_new_flags(self, _ws, _aliases):
        result = _complete(self.parser, ['new', '-'], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('-m', names)
        self.assertIn('--message', names)
        self.assertIn('-b', names)
        self.assertIn('--base-branch', names)

    def test_new_base_branch_value(self, _ws, _aliases):
        result = _complete(self.parser, ['new', '-b', ''],
                           workspace_dir='/ws')
        names = self._names(result)
        self.assertEqual(names, ['__branch__'])

    # -- update command --

    def test_update_positional(self, _ws, _aliases):
        result = _complete(self.parser, ['update', ''], workspace_dir='/ws')
        names = self._names(result)
        self.assertEqual(names, ['myalias'])

    def test_update_flags(self, _ws, _aliases):
        result = _complete(self.parser, ['update', '--'], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('--base-branch', names)
        self.assertIn('--dry-run', names)
        self.assertIn('--no-edit', names)
        self.assertIn('--shelve', names)

    def test_update_flag_value_consumed(self, _ws, _aliases):
        result = _complete(self.parser,
                           ['update', '-b', 'main', ''],
                           workspace_dir='/ws')
        names = self._names(result)
        self.assertEqual(names, ['myalias'])

    # -- list-changes command --

    def test_list_changes_flags(self, _ws, _aliases):
        result = _complete(self.parser, ['list-changes', '-'],
                           workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('-b', names)
        self.assertIn('--base-branch', names)

    # -- alias subcommands --

    def test_alias_subcommands(self, _ws, _aliases):
        result = _complete(self.parser, ['alias', ''], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('list', names)
        self.assertIn('set', names)
        self.assertIn('delete', names)
        self.assertIn('clean', names)

    def test_alias_delete_positional(self, _ws, _aliases):
        result = _complete(self.parser, ['alias', 'delete', ''],
                           workspace_dir='/ws')
        names = self._names(result)
        self.assertEqual(names, ['myalias'])

    def test_alias_set_second_positional(self, _ws, _aliases):
        result = _complete(self.parser, ['alias', 'set', '123', ''],
                           workspace_dir='/ws')
        names = self._names(result)
        self.assertEqual(names, ['myalias'])

    def test_alias_set_flags(self, _ws, _aliases):
        result = _complete(self.parser, ['alias', 'set', '-'],
                           workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('-f', names)
        self.assertIn('--force', names)


@mock.patch('git_p4son.complete.get_current_branch', return_value='feat/cool')
@mock.patch('git_p4son.complete.list_changelist_aliases',
            return_value=[('myalias', '999')])
@mock.patch('git_p4son.complete.get_workspace_dir', return_value='/ws')
class TestCompleteBranchAlias(unittest.TestCase):
    def setUp(self):
        self.parser = create_parser()

    def _names(self, candidates):
        return [name for name, _ in candidates]

    def test_new_positional_branch_keyword(self, _ws, _aliases, _branch):
        result = _complete(self.parser, ['new', 'b'], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('branch', names)

    def test_new_positional_branch_expand(self, _ws, _aliases, _branch):
        result = _complete(self.parser, ['new', 'branch'],
                           workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('feat-cool', names)
        self.assertNotIn('branch', names)

    def test_review_positional_branch_keyword(self, _ws, _aliases, _branch):
        result = _complete(self.parser, ['review', 'b'], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('branch', names)

    def test_new_positional_br_prefix(self, _ws, _aliases, _branch):
        result = _complete(self.parser, ['new', 'br'], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('branch', names)

    def test_review_positional_br_prefix(self, _ws, _aliases, _branch):
        result = _complete(self.parser, ['review', 'br'], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('branch', names)

    def test_update_positional_branch_keyword(self, _ws, _aliases, _branch):
        result = _complete(self.parser, ['update', 'b'], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('branch', names)

    def test_update_positional_br_prefix(self, _ws, _aliases, _branch):
        result = _complete(self.parser, ['update', 'br'], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('branch', names)

    def test_update_positional_branch_expand(self, _ws, _aliases, _branch):
        result = _complete(self.parser, ['update', 'branch'],
                           workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('feat-cool', names)
        self.assertNotIn('branch', names)

    def test_update_positional_includes_aliases(self, _ws, _aliases, _branch):
        result = _complete(self.parser, ['update', ''], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('branch', names)
        self.assertIn('myalias', names)

    def test_alias_set_branch_keyword(self, _ws, _aliases, _branch):
        result = _complete(self.parser, ['alias', 'set', '123', 'b'],
                           workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('branch', names)

    def test_alias_set_br_prefix(self, _ws, _aliases, _branch):
        result = _complete(self.parser, ['alias', 'set', '123', 'br'],
                           workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('branch', names)

    def test_alias_set_branch_expand(self, _ws, _aliases, _branch):
        result = _complete(self.parser, ['alias', 'set', '123', 'branch'],
                           workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('feat-cool', names)
        self.assertNotIn('branch', names)

    def test_new_positional_includes_aliases(self, _ws, _aliases, _branch):
        result = _complete(self.parser, ['new', ''], workspace_dir='/ws')
        names = self._names(result)
        self.assertIn('branch', names)
        self.assertIn('myalias', names)


@mock.patch('git_p4son.complete.get_current_branch', return_value='feat/cool')
@mock.patch('git_p4son.complete.list_changelist_aliases',
            return_value=[('myalias', '999')])
@mock.patch('git_p4son.complete.get_workspace_dir', return_value=None)
class TestCompleteBranchNoWorkspace(unittest.TestCase):
    def setUp(self):
        self.parser = create_parser()

    def _names(self, candidates):
        return [name for name, _ in candidates]

    def test_new_positional_no_branch(self, _ws, _aliases, _branch):
        result = _complete(self.parser, ['new', 'b'], workspace_dir=None)
        names = self._names(result)
        self.assertNotIn('branch', names)


@mock.patch('git_p4son.complete.get_current_branch', return_value='main')
@mock.patch('git_p4son.complete.list_changelist_aliases',
            return_value=[('myalias', '999')])
@mock.patch('git_p4son.complete.get_workspace_dir', return_value='/ws')
class TestCompleteBranchOnMain(unittest.TestCase):
    def setUp(self):
        self.parser = create_parser()

    def _names(self, candidates):
        return [name for name, _ in candidates]

    def test_new_positional_no_branch_on_main(self, _ws, _aliases, _branch):
        result = _complete(self.parser, ['new', 'b'], workspace_dir='/ws')
        names = self._names(result)
        self.assertNotIn('branch', names)


@mock.patch('git_p4son.complete.list_changelist_aliases',
            return_value=[('myalias', '999')])
@mock.patch('git_p4son.complete.get_workspace_dir', return_value=None)
class TestCompleteNoWorkspace(unittest.TestCase):
    def setUp(self):
        self.parser = create_parser()

    def _names(self, candidates):
        return [name for name, _ in candidates]

    def test_sync_no_aliases(self, _ws, _aliases):
        result = _complete(self.parser, ['sync', ''], workspace_dir=None)
        names = self._names(result)
        self.assertIn('latest', names)
        self.assertIn('last-synced', names)
        self.assertNotIn('myalias', names)

    def test_update_empty(self, _ws, _aliases):
        result = _complete(self.parser, ['update', ''], workspace_dir=None)
        names = self._names(result)
        self.assertEqual(names, [])


if __name__ == '__main__':
    unittest.main()
