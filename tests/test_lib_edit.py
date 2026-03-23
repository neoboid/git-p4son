"""Tests for edit functions in git_p4son.lib and git_p4son.perforce modules."""

import unittest
from unittest import mock

from git_p4son.common import CommandError, RunError
from git_p4son.git import (
    LocalChanges,
    find_common_ancestor,
    get_local_changes,
)
from git_p4son.lib import open_changes_for_edit
from git_p4son.perforce import (
    get_changelist_for_file,
    include_changes_in_changelist,
)
from tests.helpers import make_run_result


class TestGetChangelistForFile(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_file_not_opened(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        result = get_changelist_for_file('foo.txt', '/ws')
        self.assertIsNone(result)

    @mock.patch('git_p4son.perforce.run')
    def test_file_in_default_changelist(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/foo.txt',
            '... action edit',
            '... change default',
            '... type text',
        ])
        result = get_changelist_for_file('foo.txt', '/ws')
        self.assertEqual(result, ('default', 'edit'))

    @mock.patch('git_p4son.perforce.run')
    def test_file_in_numbered_changelist(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/foo.txt',
            '... action edit',
            '... change 12345',
            '... type text',
        ])
        result = get_changelist_for_file('foo.txt', '/ws')
        self.assertEqual(result, ('12345', 'edit'))

    @mock.patch('git_p4son.perforce.run')
    def test_file_opened_for_add(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/foo.txt',
            '... action add',
            '... change 12345',
        ])
        result = get_changelist_for_file('foo.txt', '/ws')
        self.assertEqual(result, ('12345', 'add'))

    @mock.patch('git_p4son.perforce.run')
    def test_file_opened_for_delete(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/foo.txt',
            '... action delete',
            '... change 12345',
        ])
        result = get_changelist_for_file('foo.txt', '/ws')
        self.assertEqual(result, ('12345', 'delete'))

    @mock.patch('git_p4son.perforce.run')
    def test_file_opened_for_move_add(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/foo.txt',
            '... action move/add',
            '... change 12345',
        ])
        result = get_changelist_for_file('foo.txt', '/ws')
        self.assertEqual(result, ('12345', 'move/add'))

    @mock.patch('git_p4son.perforce.run')
    def test_add_in_default_changelist(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/foo.txt',
            '... action add',
            '... change default',
        ])
        result = get_changelist_for_file('foo.txt', '/ws')
        self.assertEqual(result, ('default', 'add'))


class TestFindCommonAncestor(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_finds_ancestor(self, mock_run):
        mock_run.return_value = make_run_result(stdout=['abc123def456'])
        ancestor = find_common_ancestor('main', 'HEAD', '/ws')
        self.assertEqual(ancestor, 'abc123def456')

    @mock.patch('git_p4son.git.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = RunError('merge-base failed')
        with self.assertRaises(RunError):
            find_common_ancestor('main', 'HEAD', '/ws')

    @mock.patch('git_p4son.git.run')
    def test_no_output(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        with self.assertRaises(CommandError):
            find_common_ancestor('main', 'HEAD', '/ws')


class TestGetLocalGitChanges(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_parses_all_change_types(self, mock_run):
        mock_run.side_effect = [
            # git merge-base
            make_run_result(stdout=['abc123']),
            # git diff --name-status
            make_run_result(stdout=[
                'M\tmodified.txt',
                'A\tadded.txt',
                'D\tdeleted.txt',
                'R100\told_name.txt\tnew_name.txt',
            ]),
        ]
        changes = get_local_changes('main', '/ws')
        self.assertEqual(changes.mods, ['modified.txt'])
        self.assertEqual(changes.adds, ['added.txt'])
        self.assertEqual(changes.dels, ['deleted.txt'])
        self.assertEqual(changes.moves, [('old_name.txt', 'new_name.txt')])

    @mock.patch('git_p4son.git.run')
    def test_merge_base_failure(self, mock_run):
        mock_run.side_effect = RunError('merge-base failed')
        with self.assertRaises(RunError):
            get_local_changes('main', '/ws')

    @mock.patch('git_p4son.git.run')
    def test_unknown_status(self, mock_run):
        mock_run.side_effect = [
            make_run_result(stdout=['abc123']),
            make_run_result(stdout=['X\tunknown.txt']),
        ]
        with self.assertRaises(CommandError):
            get_local_changes('main', '/ws')


class TestIncludeChangesInChangelist(unittest.TestCase):
    # --- added files ---
    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=None)
    @mock.patch('git_p4son.perforce.run')
    def test_adds_files(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.adds = ['new_file.txt']
        include_changes_in_changelist(changes, '100', '/ws')
        mock_run.assert_called_with(
            ['p4', 'add', '-c', '100', 'new_file.txt'],
            cwd='/ws', dry_run=False,
        )

    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=('200', 'add'))
    @mock.patch('git_p4son.perforce.run')
    def test_reopens_added_file_in_different_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.adds = ['new_file.txt']
        include_changes_in_changelist(changes, '100', '/ws')
        mock_run.assert_called_with(
            ['p4', 'reopen', '-c', '100', 'new_file.txt'],
            cwd='/ws', dry_run=False,
        )

    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=('100', 'add'))
    @mock.patch('git_p4son.perforce.run')
    def test_skips_added_file_already_in_correct_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.adds = ['new_file.txt']
        include_changes_in_changelist(changes, '100', '/ws')
        mock_run.assert_not_called()

    # --- modified files ---
    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=None)
    @mock.patch('git_p4son.perforce.run')
    def test_edits_unchecked_out_file(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['mod.txt']
        include_changes_in_changelist(changes, '100', '/ws')
        mock_run.assert_called_with(
            ['p4', 'edit', '-c', '100', 'mod.txt'],
            cwd='/ws', dry_run=False,
        )

    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=('200', 'edit'))
    @mock.patch('git_p4son.perforce.run')
    def test_reopens_file_in_different_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['mod.txt']
        include_changes_in_changelist(changes, '100', '/ws')
        mock_run.assert_called_with(
            ['p4', 'reopen', '-c', '100', 'mod.txt'],
            cwd='/ws', dry_run=False,
        )

    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=('100', 'edit'))
    @mock.patch('git_p4son.perforce.run')
    def test_skips_file_already_in_correct_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['mod.txt']
        include_changes_in_changelist(changes, '100', '/ws')
        mock_run.assert_not_called()

    # --- deleted files ---
    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=None)
    @mock.patch('git_p4son.perforce.run')
    def test_deletes_files(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['old.txt']
        include_changes_in_changelist(changes, '100', '/ws')
        mock_run.assert_called_with(
            ['p4', 'delete', '-c', '100', 'old.txt'],
            cwd='/ws', dry_run=False,
        )

    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=('200', 'delete'))
    @mock.patch('git_p4son.perforce.run')
    def test_reopens_deleted_file_in_different_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['old.txt']
        include_changes_in_changelist(changes, '100', '/ws')
        mock_run.assert_called_with(
            ['p4', 'reopen', '-c', '100', 'old.txt'],
            cwd='/ws', dry_run=False,
        )

    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=('100', 'delete'))
    @mock.patch('git_p4son.perforce.run')
    def test_skips_deleted_file_already_in_correct_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['old.txt']
        include_changes_in_changelist(changes, '100', '/ws')
        mock_run.assert_not_called()

    # --- moved files ---
    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=None)
    @mock.patch('git_p4son.perforce.run')
    def test_moves_files(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.moves = [('old.txt', 'new.txt')]
        include_changes_in_changelist(changes, '100', '/ws')
        calls = mock_run.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][0], [
                         'p4', 'delete', '-c', '100', 'old.txt'])
        self.assertEqual(calls[1][0][0], ['p4', 'add', '-c', '100', 'new.txt'])

    @mock.patch('git_p4son.perforce.get_changelist_for_file',
                side_effect=[('200', 'delete'), ('200', 'add')])
    @mock.patch('git_p4son.perforce.run')
    def test_reopens_moved_files_in_different_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.moves = [('old.txt', 'new.txt')]
        include_changes_in_changelist(changes, '100', '/ws')
        calls = mock_run.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][0], [
                         'p4', 'reopen', '-c', '100', 'old.txt'])
        self.assertEqual(calls[1][0][0], [
                         'p4', 'reopen', '-c', '100', 'new.txt'])

    # --- dry run ---
    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=None)
    @mock.patch('git_p4son.perforce.run')
    def test_dry_run(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.adds = ['new.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=True)
        mock_run.assert_called_with(
            ['p4', 'add', '-c', '100', 'new.txt'],
            cwd='/ws', dry_run=True,
        )


class TestActionMismatch(unittest.TestCase):
    """Tests for reopening files when the p4 action doesn't match the desired action."""

    # --- edit -> delete ---
    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=('100', 'edit'))
    @mock.patch('git_p4son.perforce.run')
    def test_edit_to_delete_same_cl(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws')
        calls = mock_run.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][0], ['p4', 'revert', 'file.txt'])
        self.assertEqual(calls[1][0][0],
                         ['p4', 'delete', '-c', '100', 'file.txt'])

    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=('200', 'edit'))
    @mock.patch('git_p4son.perforce.run')
    def test_edit_to_delete_different_cl(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws')
        calls = mock_run.call_args_list
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0][0][0], ['p4', 'revert', 'file.txt'])
        self.assertEqual(calls[1][0][0],
                         ['p4', 'delete', '-c', '100', 'file.txt'])

    # --- delete -> edit ---
    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=('100', 'delete'))
    @mock.patch('git_p4son.perforce.run')
    def test_delete_to_edit_same_cl(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws')
        calls = mock_run.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][0][0], ['p4', 'revert', 'file.txt'])
        self.assertEqual(calls[1][0][0],
                         ['p4', 'edit', '-c', '100', 'file.txt'])
        self.assertEqual(calls[2][0][0],
                         ['git', 'restore', 'file.txt'])

    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=('200', 'delete'))
    @mock.patch('git_p4son.perforce.run')
    def test_delete_to_edit_different_cl(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws')
        calls = mock_run.call_args_list
        self.assertEqual(len(calls), 3)
        self.assertEqual(calls[0][0][0], ['p4', 'revert', 'file.txt'])
        self.assertEqual(calls[1][0][0],
                         ['p4', 'edit', '-c', '100', 'file.txt'])
        self.assertEqual(calls[2][0][0],
                         ['git', 'restore', 'file.txt'])

    # --- add -> delete (revert only, no reopen) ---
    @mock.patch('git_p4son.perforce.get_changelist_for_file', return_value=('100', 'add'))
    @mock.patch('git_p4son.perforce.run')
    def test_add_to_delete_reverts_only(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws')
        calls = mock_run.call_args_list
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0][0], ['p4', 'revert', 'file.txt'])


class TestOpenChangesForEdit(unittest.TestCase):
    @mock.patch('git_p4son.lib.include_changes_in_changelist')
    @mock.patch('git_p4son.lib.get_local_changes')
    def test_success(self, mock_get_changes, mock_include):
        mock_changes = mock.Mock()
        mock_get_changes.return_value = mock_changes
        open_changes_for_edit('100', 'HEAD~1', '/ws')
        mock_include.assert_called_once_with(mock_changes, '100', '/ws', False)

    @mock.patch('git_p4son.lib.get_local_changes')
    def test_get_changes_failure(self, mock_get_changes):
        mock_get_changes.side_effect = RunError('get changes failed')
        with self.assertRaises(RunError):
            open_changes_for_edit('100', 'HEAD~1', '/ws')


if __name__ == '__main__':
    unittest.main()
