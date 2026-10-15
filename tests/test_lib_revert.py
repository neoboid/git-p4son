"""Tests for reverting unchanged files in git_p4son.lib and git_p4son.perforce."""

import os
import tempfile
import unittest
from unittest import mock

from git_p4son.lib import DRY_RUN_CHANGELIST, revert_unchanged_files


class TestRevertUnchangedFiles(unittest.TestCase):
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
            'revert': mock.patch('git_p4son.lib.p4_revert'),
            'restore': mock.patch('git_p4son.lib.restore_files'),
            'writable_mode': mock.patch(
                'git_p4son.lib.is_writable_mode', return_value=False),
            'make_writable': mock.patch('git_p4son.writable.make_writable'),
            'log': mock.patch('git_p4son.lib.log'),
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

    def test_tracked_edit_is_revert_candidate(self):
        self._setup([('a.txt', 'edit')], ['a.txt'], ['a.txt'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.assertEqual(self._unchanged_candidates(), ['a.txt'])
        self.mocks['revert_unchanged'].assert_called_once_with(
            ['a.txt'], '100', self.ws, False)

    def test_untracked_edit_is_never_reverted(self):
        """A p4-only file checked out by hand stays opened, even when unchanged."""
        self._setup([('Asset.uasset', 'edit')], [], ['Asset.uasset'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.assertEqual(self._unchanged_candidates(), [])
        self.mocks['revert'].assert_not_called()

    def test_tracked_deletes_present_on_disk_are_reverted_and_restored(self):
        self._setup([('a.txt', 'delete'), ('b.txt', 'delete')],
                    ['a.txt', 'b.txt'], ['a.txt', 'b.txt'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.mocks['revert'].assert_called_once_with(
            ['a.txt', 'b.txt'], self.ws, False)
        self.mocks['restore'].assert_called_once_with(
            ['a.txt', 'b.txt'], self.ws, False)
        self.mocks['log'].success.assert_called_once_with('2 files reverted')

    def test_real_delete_is_left_alone(self):
        """Opened for delete and gone from git and disk: a real delete."""
        self._setup([('gone.txt', 'delete')], [], [])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.mocks['revert'].assert_not_called()
        self.assertEqual(self._unchanged_candidates(), [])

    def test_missing_add_is_revert_candidate_tracked_or_not(self):
        self._setup([('was_added.txt', 'add'), ('tracked_add.txt', 'add')],
                    ['tracked_add.txt'], [])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.assertEqual(self._unchanged_candidates(),
                         ['was_added.txt', 'tracked_add.txt'])

    def test_untracked_add_present_on_disk_is_left_alone(self):
        self._setup([('New.uasset', 'add')], [], ['New.uasset'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.assertEqual(self._unchanged_candidates(), [])
        self.mocks['revert'].assert_not_called()

    def test_tracked_add_present_on_disk_is_left_alone(self):
        """A real new file stays opened; there is no depot version to compare with."""
        self._setup([('new.txt', 'add')], ['new.txt'], ['new.txt'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.assertEqual(self._unchanged_candidates(), [])

    def test_other_actions_are_left_alone(self):
        self._setup([('m.txt', 'move/add'), ('d.txt', 'move/delete'),
                     ('i.txt', 'integrate'), ('b.txt', 'branch')],
                    ['m.txt', 'i.txt', 'b.txt'],
                    ['m.txt', 'i.txt', 'b.txt'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.assertEqual(self._unchanged_candidates(), [])
        self.mocks['revert'].assert_not_called()

    def test_counts_reverted_files(self):
        self._setup([('a.txt', 'edit'), ('b.txt', 'edit'),
                     ('c.txt', 'delete')],
                    ['a.txt', 'b.txt', 'c.txt'], ['a.txt', 'b.txt', 'c.txt'])
        self.mocks['revert_unchanged'].return_value = [
            '//depot/a.txt#1 - was edit, reverted']
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.mocks['log'].success.assert_called_once_with('2 files reverted')

    def test_dry_run_reverts_and_restores_nothing(self):
        self._setup([('a.txt', 'edit'), ('c.txt', 'delete')],
                    ['a.txt', 'c.txt'], ['a.txt', 'c.txt'])
        revert_unchanged_files('100', self.ws, dry_run=True)
        self.mocks['revert_unchanged'].assert_called_once_with(
            ['a.txt'], '100', self.ws, True)
        self.mocks['revert'].assert_called_once_with(['c.txt'], self.ws, True)

    def test_dry_run_placeholder_changelist_queries_nothing(self):
        revert_unchanged_files(DRY_RUN_CHANGELIST, self.ws, dry_run=True)
        self.mocks['opened'].assert_not_called()
        self.mocks['revert_unchanged'].assert_not_called()

    def test_writable_mode_makes_tracked_files_writable(self):
        self.mocks['writable_mode'].return_value = True
        self._setup([('a.txt', 'edit'), ('c.txt', 'delete'),
                     ('gone.txt', 'add')],
                    ['a.txt', 'c.txt'], ['a.txt', 'c.txt'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.mocks['make_writable'].assert_called_once_with([
            os.path.join(self.ws, 'a.txt'),
            os.path.join(self.ws, 'c.txt'),
        ])

    def test_writable_mode_off_leaves_permissions(self):
        self._setup([('a.txt', 'edit')], ['a.txt'], ['a.txt'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.mocks['make_writable'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
