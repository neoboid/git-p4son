"""Tests for reverting stale files in git_p4son.lib and git_p4son.perforce."""

import os
import tempfile
import unittest
from unittest import mock

from git_p4son.lib import revert_stale_files
from git_p4son.perforce import (
    get_opened_files_in_changelist,
    p4_revert_unchanged,
)
from tests.helpers import make_run_result


class TestGetOpenedFilesInChangelist(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_filters_to_changelist(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... path /ws/a.txt',
            '... action edit',
            '... change 100',
            '',
            '... path /ws/other.txt',
            '... action edit',
            '... change 200',
            '',
            '... path /ws/sub/b.txt',
            '... action add',
            '... change 100',
        ])
        files = get_opened_files_in_changelist('100', '/ws')
        self.assertEqual(files, [('a.txt', 'edit'), ('sub/b.txt', 'add')])
        self.assertEqual(
            mock_run.call_args.args[0],
            ['p4', '-ztag', 'fstat', '-Ro', '-Op',
             '-T', 'path,clientFile,action,change', '...'])

    @mock.patch('git_p4son.perforce.run')
    def test_skips_files_outside_workspace(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... path /elsewhere/a.txt',
            '... action edit',
            '... change 100',
        ])
        self.assertEqual(get_opened_files_in_changelist('100', '/ws'), [])

    @mock.patch('git_p4son.perforce.run')
    def test_windows_paths(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... path D:\\p4\\Games\\Script\\a.as',
            '... action edit',
            '... change 100',
        ])
        files = get_opened_files_in_changelist('100', 'D:\\p4\\Games')
        self.assertEqual(files, [('Script/a.as', 'edit')])

    @mock.patch('git_p4son.perforce.run')
    def test_record_without_path_is_skipped(self, mock_run):
        """Without -Op fstat reports no path; such records must not
        crash, and clientFile syntax is never inside the workspace."""
        mock_run.return_value = make_run_result(stdout=[
            '... clientFile //client/a.txt',
            '... action edit',
            '... change 100',
            '',
            '... action edit',
            '... change 100',
        ])
        self.assertEqual(get_opened_files_in_changelist('100', '/ws'), [])

    @mock.patch('git_p4son.perforce.log')
    @mock.patch('git_p4son.perforce.run')
    def test_nothing_opened(self, mock_run, mock_log):
        mock_run.return_value = make_run_result(
            returncode=1, stderr=['... - file(s) not opened on this client.'])
        self.assertEqual(get_opened_files_in_changelist('100', '/ws'), [])
        mock_log.warning.assert_not_called()

    @mock.patch('git_p4son.perforce.log')
    @mock.patch('git_p4son.perforce.run')
    def test_error_is_warned_about(self, mock_run, mock_log):
        mock_run.return_value = make_run_result(
            returncode=1, stderr=['Perforce client error: connect failed'])
        self.assertEqual(get_opened_files_in_changelist('100', '/ws'), [])
        mock_log.warning.assert_called_once()


class TestP4RevertUnchanged(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_no_files_runs_nothing(self, mock_run):
        self.assertEqual(p4_revert_unchanged([], '100', '/ws'), [])
        mock_run.assert_not_called()

    @mock.patch('git_p4son.perforce.run')
    def test_reverts_via_stdin_file_list(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '//depot/a.txt#3 - was edit, reverted',
        ])
        reverted = p4_revert_unchanged(['a.txt', 'b.txt'], '100', '/ws')
        self.assertEqual(reverted, ['//depot/a.txt#3 - was edit, reverted'])
        mock_run.assert_called_once_with(
            ['p4', '-x', '-', 'revert', '-a', '-c', '100'],
            cwd='/ws', input='a.txt\nb.txt', fail_on_returncode=False)

    @mock.patch('git_p4son.perforce.run')
    def test_dry_run_previews(self, mock_run):
        """-n is a read-only preview, so it really runs on dry run."""
        mock_run.return_value = make_run_result()
        p4_revert_unchanged(['a.txt'], '100', '/ws', dry_run=True)
        mock_run.assert_called_once_with(
            ['p4', '-x', '-', 'revert', '-a', '-n', '-c', '100'],
            cwd='/ws', input='a.txt', fail_on_returncode=False)


class TestRevertStaleFiles(unittest.TestCase):
    """The tracked/untracked boundary decides what may be reverted."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ws = self.tmp.name
        patches = {
            'opened': mock.patch(
                'git_p4son.lib.get_opened_files_in_changelist'),
            'tracked': mock.patch('git_p4son.lib.get_tracked_files'),
            'revert_unchanged': mock.patch(
                'git_p4son.lib.p4_revert_unchanged', return_value=[]),
            'run': mock.patch('git_p4son.lib.run'),
            'writable_mode': mock.patch(
                'git_p4son.lib.is_writable_mode', return_value=False),
            'make_writable': mock.patch('git_p4son.lib.make_writable'),
        }
        self.mocks = {name: p.start() for name, p in patches.items()}
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.tmp.cleanup)

    def _setup(self, opened, tracked, on_disk):
        self.mocks['opened'].return_value = opened
        self.mocks['tracked'].return_value = set(tracked)
        for path in on_disk:
            with open(os.path.join(self.ws, path), 'w') as f:
                f.write('content')

    def _unchanged_candidates(self):
        return self.mocks['revert_unchanged'].call_args.args[0]

    def _run_commands(self):
        return [c.args[0] for c in self.mocks['run'].call_args_list]

    def test_tracked_edit_is_revert_candidate(self):
        self._setup([('a.txt', 'edit')], ['a.txt'], ['a.txt'])
        revert_stale_files('100', self.ws)
        self.assertEqual(self._unchanged_candidates(), ['a.txt'])
        self.mocks['revert_unchanged'].assert_called_once_with(
            ['a.txt'], '100', self.ws, False)

    def test_untracked_edit_is_never_reverted(self):
        """A p4-only binary checked out by hand stays opened, even when
        unchanged."""
        self._setup([('Asset.uasset', 'edit')], [], ['Asset.uasset'])
        revert_stale_files('100', self.ws)
        self.assertEqual(self._unchanged_candidates(), [])
        self.assertEqual(self._run_commands(), [])

    def test_tracked_delete_present_on_disk_is_reverted_and_restored(self):
        self._setup([('a.txt', 'delete')], ['a.txt'], ['a.txt'])
        count = revert_stale_files('100', self.ws)
        self.assertEqual(self._run_commands(), [
            ['p4', 'revert', 'a.txt'],
            ['git', 'restore', 'a.txt'],
        ])
        self.assertEqual(count, 1)

    def test_real_delete_is_left_alone(self):
        """Opened for delete and gone from git and disk: a real delete."""
        self._setup([('gone.txt', 'delete')], [], [])
        revert_stale_files('100', self.ws)
        self.assertEqual(self._run_commands(), [])
        self.assertEqual(self._unchanged_candidates(), [])

    def test_missing_add_is_revert_candidate_tracked_or_not(self):
        self._setup([('was_added.txt', 'add'), ('tracked_add.txt', 'add')],
                    ['tracked_add.txt'], [])
        revert_stale_files('100', self.ws)
        self.assertEqual(self._unchanged_candidates(),
                         ['was_added.txt', 'tracked_add.txt'])

    def test_untracked_add_present_on_disk_is_left_alone(self):
        self._setup([('New.uasset', 'add')], [], ['New.uasset'])
        revert_stale_files('100', self.ws)
        self.assertEqual(self._unchanged_candidates(), [])
        self.assertEqual(self._run_commands(), [])

    def test_tracked_add_present_on_disk_is_left_alone(self):
        """A real new file: there is no depot version to be unchanged
        against."""
        self._setup([('new.txt', 'add')], ['new.txt'], ['new.txt'])
        revert_stale_files('100', self.ws)
        self.assertEqual(self._unchanged_candidates(), [])

    def test_other_actions_are_left_alone(self):
        self._setup([('m.txt', 'move/add'), ('d.txt', 'move/delete'),
                     ('i.txt', 'integrate'), ('b.txt', 'branch')],
                    ['m.txt', 'i.txt', 'b.txt'],
                    ['m.txt', 'i.txt', 'b.txt'])
        revert_stale_files('100', self.ws)
        self.assertEqual(self._unchanged_candidates(), [])
        self.assertEqual(self._run_commands(), [])

    def test_counts_reverted_files(self):
        self._setup([('a.txt', 'edit'), ('b.txt', 'edit'),
                     ('c.txt', 'delete')],
                    ['a.txt', 'b.txt', 'c.txt'], ['a.txt', 'b.txt', 'c.txt'])
        self.mocks['revert_unchanged'].return_value = [
            '//depot/a.txt#1 - was edit, reverted']
        self.assertEqual(revert_stale_files('100', self.ws), 2)

    def test_dry_run_reverts_and_restores_nothing(self):
        self._setup([('a.txt', 'edit'), ('c.txt', 'delete')],
                    ['a.txt', 'c.txt'], ['a.txt', 'c.txt'])
        revert_stale_files('100', self.ws, dry_run=True)
        self.mocks['revert_unchanged'].assert_called_once_with(
            ['a.txt'], '100', self.ws, True)
        for call in self.mocks['run'].call_args_list:
            self.assertTrue(call.kwargs['dry_run'])

    def test_dry_run_placeholder_changelist_queries_nothing(self):
        count = revert_stale_files('<changelist>', self.ws, dry_run=True)
        self.assertEqual(count, 0)
        self.mocks['opened'].assert_not_called()
        self.mocks['revert_unchanged'].assert_not_called()

    def test_writable_mode_makes_tracked_files_writable(self):
        self.mocks['writable_mode'].return_value = True
        self._setup([('a.txt', 'edit'), ('c.txt', 'delete'),
                     ('gone.txt', 'add')],
                    ['a.txt', 'c.txt'], ['a.txt', 'c.txt'])
        revert_stale_files('100', self.ws)
        self.mocks['make_writable'].assert_called_once_with([
            os.path.join(self.ws, 'a.txt'),
            os.path.join(self.ws, 'c.txt'),
        ])

    def test_writable_mode_off_leaves_permissions(self):
        self._setup([('a.txt', 'edit')], ['a.txt'], ['a.txt'])
        revert_stale_files('100', self.ws)
        self.mocks['make_writable'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
