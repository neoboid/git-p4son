"""Tests for edit functions in git_p4son.lib and git_p4son.perforce modules."""

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from git_p4son.common import CommandError, RunError
from git_p4son.git import (
    LocalChanges,
    get_local_changes,
)
from git_p4son.lib import (
    include_changes_in_changelist,
    open_changes_and_revert_unchanged,
)
from git_p4son.perforce import get_opened_files, opened_lookup
from tests.helpers import make_run_result


def _commands(mock_run):
    """The (command, stdin) of each run() call."""
    return [(c.args[0], c.kwargs.get('input')) for c in mock_run.call_args_list]


class TestGetOpenedFiles(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_one_query_for_all_files(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... path /ws/a.txt',
            '... action edit',
            '... change 100',
            '',
            '... path /ws/sub/b.txt',
            '... action add',
            '... change default',
        ])
        opened = get_opened_files(['a.txt', 'sub/b.txt', 'c.txt'], '/ws')
        self.assertEqual(opened, {'a.txt': ('100', 'edit'),
                                  'sub/b.txt': ('default', 'add')})
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.args[0],
                         ['p4', '-L', 'en', '-x', '-', '-ztag', 'fstat', '-Ro', '-Op',
                          '-T', 'path,clientFile,action,change'])
        self.assertEqual(mock_run.call_args.kwargs['input'],
                         'a.txt\nsub/b.txt\nc.txt')

    @mock.patch('git_p4son.perforce.run')
    def test_no_files_makes_no_query(self, mock_run):
        self.assertEqual(get_opened_files([], '/ws'), {})
        mock_run.assert_not_called()

    @mock.patch('git_p4son.perforce.log')
    @mock.patch('git_p4son.perforce.run')
    def test_unopened_and_new_files_are_not_errors(self, mock_run, mock_log):
        mock_run.return_value = make_run_result(returncode=1, stderr=[
            'c.txt - file(s) not opened on this client.',
            'new.txt - no such file(s).',
        ])
        self.assertEqual(get_opened_files(['c.txt', 'new.txt'], '/ws'), {})
        mock_log.warning.assert_not_called()

    @mock.patch('git_p4son.perforce.log')
    @mock.patch('git_p4son.perforce.run')
    def test_other_errors_warn(self, mock_run, mock_log):
        mock_run.return_value = make_run_result(
            returncode=1, stderr=['Perforce password (P4PASSWD) invalid.'])
        self.assertEqual(get_opened_files(['a.txt'], '/ws'), {})
        mock_log.warning.assert_called_once()

    @mock.patch('git_p4son.perforce.run')
    def test_windows_paths(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... path D:\\p4\\Games\\Script\\a.as',
            '... action edit',
            '... change 100',
        ])
        opened = get_opened_files(['Script/a.as'], 'D:\\p4\\Games')
        self.assertEqual(opened, {'Script/a.as': ('100', 'edit')})


class TestOpenedLookup(unittest.TestCase):
    def test_exact_match(self):
        lookup = opened_lookup({'a.txt': ('1', 'edit')})
        self.assertEqual(lookup('a.txt'), ('1', 'edit'))

    def test_case_differs(self):
        """p4 may spell the path in the client's case."""
        lookup = opened_lookup({'Source/A.cpp': ('1', 'edit')})
        self.assertEqual(lookup('source/a.cpp'), ('1', 'edit'))

    def test_exact_match_wins_over_case_fold(self):
        lookup = opened_lookup({'a.txt': ('1', 'edit'),
                                'A.txt': ('2', 'add')})
        self.assertEqual(lookup('A.txt'), ('2', 'add'))

    def test_not_opened(self):
        self.assertIsNone(opened_lookup({'a.txt': ('1', 'edit')})('b.txt'))


class TestGetLocalGitChanges(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_parses_all_change_types(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=b'M\0modified.txt\0A\0added.txt\0D\0deleted.txt\0R100\0old_name.txt\0new_name.txt\0')
        changes = get_local_changes('main', '/ws')
        self.assertEqual(changes.mods, ['modified.txt'])
        self.assertEqual(changes.adds, ['added.txt'])
        self.assertEqual(changes.dels, ['deleted.txt'])
        self.assertEqual(changes.moves, [('old_name.txt', 'new_name.txt')])
        self.assertEqual(mock_run.call_args.args[0],
                         ['git', 'diff', '--name-status', '-z', 'main...HEAD'])

    @mock.patch('git_p4son.git.run')
    def test_typechange_treated_as_modify(self, mock_run):
        """T (e.g. symlink to regular file) is a content change."""
        mock_run.return_value = make_run_result(stdout=b'T\0link.txt\0')
        changes = get_local_changes('main', '/ws')
        self.assertEqual(changes.mods, ['link.txt'])

    @mock.patch('git_p4son.git.run')
    def test_copy_treated_as_add_of_destination(self, mock_run):
        """A copy is an add of the destination; the source is untouched."""
        mock_run.return_value = make_run_result(
            stdout=b'C100\0src.txt\0copy.txt\0')
        changes = get_local_changes('main', '/ws')
        self.assertEqual(changes.adds, ['copy.txt'])
        self.assertEqual(changes.mods, [])
        self.assertEqual(changes.dels, [])

    @mock.patch('git_p4son.git.run')
    def test_diff_failure(self, mock_run):
        mock_run.side_effect = RunError('diff failed')
        with self.assertRaises(RunError):
            get_local_changes('main', '/ws')

    @mock.patch('git_p4son.git.run')
    def test_unknown_status(self, mock_run):
        mock_run.return_value = make_run_result(stdout=b'X\0unknown.txt\0')
        with self.assertRaises(CommandError):
            get_local_changes('main', '/ws')


class TestGetLocalChangesInRepo(unittest.TestCase):
    def test_changes_on_the_base_branch_since_the_fork_are_left_out(self):
        with tempfile.TemporaryDirectory() as ws:
            def git(*args):
                subprocess.run(['git', *args], cwd=ws,
                               capture_output=True, check=True)

            def write(name):
                with open(os.path.join(ws, name), 'w') as f:
                    f.write(name)

            git('init', '-b', 'main')
            git('config', 'user.email', 't@t')
            git('config', 'user.name', 'T')
            git('commit', '--allow-empty', '-m', 'base')
            git('switch', '-c', 'feat')
            write('feature.txt')
            git('add', '.')
            git('commit', '-m', 'feature')
            git('switch', 'main')
            write('upstream.txt')
            git('add', '.')
            git('commit', '-m', 'upstream')
            git('switch', 'feat')

            changes = get_local_changes('main', ws)

        self.assertEqual(changes.adds, ['feature.txt'])
        self.assertEqual(changes.dels, [])

    def test_non_ascii_filename_returned_verbatim(self):
        """Non-ASCII filenames are returned verbatim, not C-quoted."""
        with tempfile.TemporaryDirectory() as ws:
            def git(*args):
                subprocess.run(['git', *args], cwd=ws,
                               capture_output=True, check=True)
            git('init')
            git('config', 'user.email', 't@t')
            git('config', 'user.name', 'T')
            git('commit', '--allow-empty', '-m', 'base')
            base = subprocess.run(
                ['git', 'rev-parse', 'HEAD'], cwd=ws,
                capture_output=True, text=True, check=True).stdout.strip()
            with open(os.path.join(ws, 'bäck.txt'), 'w') as f:
                f.write('content')
            git('add', '.')
            git('commit', '-m', 'add file')

            changes = get_local_changes(base, ws)

        self.assertEqual(changes.adds, ['bäck.txt'])

    def test_special_characters_returned_verbatim(self):
        """Quotes, backslashes and tabs, which git otherwise C-quotes, come back as the real names."""
        names = ['we"ird.txt', 'back\\slash.txt', 'tab\tname.txt']
        with tempfile.TemporaryDirectory() as ws:
            def git(*args):
                subprocess.run(['git', *args], cwd=ws,
                               capture_output=True, check=True)
            git('init')
            git('config', 'user.email', 't@t')
            git('config', 'user.name', 'T')
            with open(os.path.join(ws, 'old.txt'), 'w') as f:
                f.write('content to rename\n' * 10)
            git('add', '.')
            git('commit', '-m', 'base')
            base = subprocess.run(
                ['git', 'rev-parse', 'HEAD'], cwd=ws,
                capture_output=True, text=True, check=True).stdout.strip()
            for name in names:
                with open(os.path.join(ws, name), 'w') as f:
                    f.write(name)
            git('mv', 'old.txt', 'new "name".txt')
            git('add', '.')
            git('commit', '-m', 'add files')

            changes = get_local_changes(base, ws)

        self.assertEqual(sorted(changes.adds), sorted(names))
        self.assertEqual(changes.moves, [('old.txt', 'new "name".txt')])


class TestOpenWarnings(unittest.TestCase):
    """Files p4 did not open are found by asking again, since p4 exits 0 for most of them."""

    def _run(self, adds, opened_after, output):
        changes = LocalChanges()
        changes.adds = adds
        with mock.patch('git_p4son.lib.get_opened_files',
                        side_effect=[{}, opened_after]) as mock_opened, \
                mock.patch('git_p4son.perforce.run',
                           return_value=make_run_result(
                               returncode=1, stdout=output)), \
                mock.patch('git_p4son.perforce.log'), \
                mock.patch('git_p4son.lib.log') as mock_log:
            include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        return mock_opened, mock_log

    def test_warns_about_files_left_unopened(self):
        """A file matching .p4ignore is skipped with a warning and the rest still open."""
        _, mock_log = self._run(
            ['generated.cs', 'real.txt'], {'real.txt': ('100', 'add')},
            ['//depot/real.txt#1 - opened for add',
             "generated.cs - ignored file can't be added."])
        mock_log.warning.assert_called_once_with(
            'p4 did not open 1 of 2 files:')
        logged = [c.args[0] for c in mock_log.info.call_args_list]
        self.assertIn('  generated.cs (for add)', logged)
        self.assertIn("  generated.cs - ignored file can't be added.", logged)
        self.assertNotIn('  //depot/real.txt#1 - opened for add', logged)

    def test_file_opened_in_another_changelist_warns(self):
        _, mock_log = self._run(['a.txt'], {'a.txt': ('200', 'add')}, [])
        mock_log.warning.assert_called_once()

    def test_no_warning_when_all_opened(self):
        mock_opened, mock_log = self._run(
            ['new.txt'], {'new.txt': ('100', 'add')},
            ['//depot/new.txt#1 - opened for add'])
        mock_log.warning.assert_not_called()
        self.assertEqual(mock_opened.call_args_list[1].args,
                         (['new.txt'], '/ws'))

    @mock.patch('git_p4son.perforce.run', return_value=make_run_result())
    @mock.patch('git_p4son.lib.get_opened_files', return_value={})
    def test_dry_run_does_not_ask_again(self, mock_opened, _run):
        changes = LocalChanges()
        changes.adds = ['new.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=True)
        mock_opened.assert_called_once()


class TestIncludeChangesInChangelist(unittest.TestCase):

    def setUp(self):
        self.mocks = {}
        for target, value in (('get_opened_files', {}),
                              ('_warn_unopened', None),
                              ('restore_files', None)):
            patcher = mock.patch(f'git_p4son.lib.{target}',
                                 return_value=value)
            self.mocks[target] = patcher.start()
            self.addCleanup(patcher.stop)
    # --- added files ---

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=None))
    @mock.patch('git_p4son.perforce.run')
    def test_adds_files(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.adds = ['new_file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'add', '-c', '100'], 'new_file.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('200', 'add')))
    @mock.patch('git_p4son.perforce.run')
    def test_reopens_added_file_in_different_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.adds = ['new_file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'reopen', '-c', '100'], 'new_file.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('100', 'add')))
    @mock.patch('git_p4son.perforce.run')
    def test_skips_added_file_already_in_correct_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.adds = ['new_file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        mock_run.assert_not_called()

    # --- modified files ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=None))
    @mock.patch('git_p4son.perforce.run')
    def test_edits_unchecked_out_file(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['mod.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'edit', '-c', '100'], 'mod.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('200', 'edit')))
    @mock.patch('git_p4son.perforce.run')
    def test_reopens_file_in_different_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['mod.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'reopen', '-c', '100'], 'mod.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('100', 'edit')))
    @mock.patch('git_p4son.perforce.run')
    def test_skips_file_already_in_correct_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['mod.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        mock_run.assert_not_called()

    # --- deleted files ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=None))
    @mock.patch('git_p4son.perforce.run')
    def test_deletes_files(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['old.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'delete', '-c', '100'], 'old.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('200', 'delete')))
    @mock.patch('git_p4son.perforce.run')
    def test_reopens_deleted_file_in_different_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['old.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'reopen', '-c', '100'], 'old.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('100', 'delete')))
    @mock.patch('git_p4son.perforce.run')
    def test_skips_deleted_file_already_in_correct_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['old.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        mock_run.assert_not_called()

    # --- moved files ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=None))
    @mock.patch('git_p4son.perforce.run')
    def test_moves_files(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.moves = [('old.txt', 'new.txt')]
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'add', '-c', '100'], 'new.txt'),
            (['p4', '-L', 'en', '-x', '-', 'delete', '-c', '100'], 'old.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(side_effect=[('200', 'delete'), ('200', 'add')]))
    @mock.patch('git_p4son.perforce.run')
    def test_reopens_moved_files_in_different_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.moves = [('old.txt', 'new.txt')]
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'reopen', '-c', '100'], 'old.txt\nnew.txt')])

    # --- dry run ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=None))
    @mock.patch('git_p4son.perforce.run')
    def test_dry_run(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.adds = ['new.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=True)
        mock_run.assert_called_once_with(
            ['p4', '-L', 'en', '-x', '-', 'add', '-c', '100'], cwd='/ws',
            input='new.txt', dry_run=True, fail_on_returncode=False)


class TestOneOpenedQuery(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run', return_value=make_run_result())
    @mock.patch('git_p4son.lib.get_opened_files', return_value={})
    def test_every_path_in_one_query(self, mock_opened, _run):
        changes = LocalChanges()
        changes.adds = ['new.txt']
        changes.mods = ['mod.txt']
        changes.dels = ['gone.txt']
        changes.moves = [('old.txt', 'renamed.txt')]
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(
            mock_opened.call_args_list[0].args,
            (['new.txt', 'mod.txt', 'gone.txt', 'old.txt', 'renamed.txt'], '/ws'))


class TestActionMismatch(unittest.TestCase):
    """Tests for reopening files when the p4 action doesn't match the desired action."""

    def setUp(self):
        self.mocks = {}
        for target, value in (('get_opened_files', {}),
                              ('_warn_unopened', None),
                              ('restore_files', None)):
            patcher = mock.patch(f'git_p4son.lib.{target}',
                                 return_value=value)
            self.mocks[target] = patcher.start()
            self.addCleanup(patcher.stop)

    # --- edit -> delete ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('100', 'edit')))
    @mock.patch('git_p4son.perforce.run')
    def test_edit_to_delete_same_cl(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'revert'], 'file.txt'),
            (['p4', '-L', 'en', '-x', '-', 'delete', '-c', '100'], 'file.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('200', 'edit')))
    @mock.patch('git_p4son.perforce.run')
    def test_edit_to_delete_different_cl(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'revert'], 'file.txt'),
            (['p4', '-L', 'en', '-x', '-', 'delete', '-c', '100'], 'file.txt')])

    # --- delete -> edit ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('100', 'delete')))
    @mock.patch('git_p4son.perforce.run')
    def test_delete_to_edit_same_cl(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'revert'], 'file.txt'),
            (['p4', '-L', 'en', '-x', '-', 'edit', '-c', '100'], 'file.txt')])
        self.mocks['restore_files'].assert_called_once_with(
            ['file.txt'], '/ws', False)

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('200', 'delete')))
    @mock.patch('git_p4son.perforce.run')
    def test_delete_to_edit_different_cl(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'revert'], 'file.txt'),
            (['p4', '-L', 'en', '-x', '-', 'edit', '-c', '100'], 'file.txt')])
        self.mocks['restore_files'].assert_called_once_with(
            ['file.txt'], '/ws', False)

    # --- add -> edit (keep as add, file is new to depot) ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('100', 'add')))
    @mock.patch('git_p4son.perforce.run')
    def test_add_to_edit_same_cl_keeps_add(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        mock_run.assert_not_called()

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('200', 'add')))
    @mock.patch('git_p4son.perforce.run')
    def test_add_to_edit_different_cl_reopens(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'reopen', '-c', '100'], 'file.txt')])

    # --- add -> delete (revert only, no reopen) ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('100', 'add')))
    @mock.patch('git_p4son.perforce.run')
    def test_add_to_delete_reverts_only(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
                         (['p4', '-L', 'en', '-x', '-', 'revert'], 'file.txt')])


class TestBatchedActions(unittest.TestCase):
    @mock.patch('git_p4son.lib.restore_files')
    @mock.patch('git_p4son.lib._warn_unopened')
    @mock.patch('git_p4son.perforce.log')
    @mock.patch('git_p4son.perforce.run', return_value=make_run_result())
    @mock.patch('git_p4son.lib.get_opened_files', return_value={
        'a.txt': ('100', 'delete'), 'b.txt': ('200', 'delete'),
        'c.txt': ('200', 'edit'), 'd.txt': ('300', 'edit')})
    def test_reverts_reopens_and_restores_in_one_call_each(
            self, _opened, mock_run, mock_log, _warn, mock_restore):
        changes = LocalChanges()
        changes.mods = ['a.txt', 'b.txt', 'c.txt', 'd.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'revert'], 'a.txt\nb.txt'),
            (['p4', '-L', 'en', '-x', '-', 'edit', '-c', '100'], 'a.txt\nb.txt'),
            (['p4', '-L', 'en', '-x', '-', 'reopen', '-c', '100'], 'c.txt\nd.txt'),
        ])
        mock_restore.assert_called_once_with(['a.txt', 'b.txt'], '/ws', False)
        logged = [c.args[0] for c in mock_log.info.call_args_list]
        self.assertIn('Moving files from other changelists:', logged)
        self.assertIn('  c.txt', logged)

    @mock.patch('git_p4son.perforce.run', return_value=make_run_result())
    @mock.patch('git_p4son.lib.get_opened_files',
                return_value={'a.txt': ('200', 'delete')})
    def test_dry_run_reaches_every_batch(self, _opened, mock_run):
        changes = LocalChanges()
        changes.mods = ['a.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=True)
        self.assertTrue(all(c.kwargs['dry_run']
                            for c in mock_run.call_args_list))


@mock.patch('git_p4son.lib.revert_unchanged_files')
class TestOpenChangesAndRevertUnchanged(unittest.TestCase):
    @mock.patch('git_p4son.lib.include_changes_in_changelist')
    @mock.patch('git_p4son.lib.get_local_changes')
    def test_opens_the_local_changes_then_reverts(self, mock_get_changes,
                                                  mock_include, mock_revert):
        mock_changes = mock.Mock()
        mock_get_changes.return_value = mock_changes
        open_changes_and_revert_unchanged('100', 'HEAD~1', '/ws', False)
        mock_get_changes.assert_called_once_with('HEAD~1', '/ws')
        mock_include.assert_called_once_with(mock_changes, '100', '/ws', False)
        mock_revert.assert_called_once_with('100', '/ws', False)

    @mock.patch('git_p4son.lib.get_local_changes')
    def test_get_changes_failure(self, mock_get_changes, mock_revert):
        mock_get_changes.side_effect = RunError('get changes failed')
        with self.assertRaises(RunError):
            open_changes_and_revert_unchanged('100', 'HEAD~1', '/ws', False)
        mock_revert.assert_not_called()


if __name__ == '__main__':
    unittest.main()
