"""Tests for the sync progress file and its recorder."""

import datetime
import os
import tomllib
import unittest
from unittest import mock

from git_p4son.perforce import P4SyncPreviewFile
from git_p4son.sync_merge import ChangedFile, WritableSyncFileSet
from git_p4son.sync_progress import (
    LocalChange, MergedFile, NoProgress, PassLists, ProgressPass, ProgressRecorder, SyncProgress, format_sync_progress,
    progress_path, read_sync_progress, remove_sync_progress, write_sync_progress,
)
from tests.helpers import WorkspaceTestCase

STARTED = datetime.datetime(2026, 11, 25, 9, 12, 44,
                            tzinfo=datetime.timezone.utc)


def written(workspace_dir: str) -> SyncProgress:
    """The sync progress written in workspace_dir, which must exist."""
    read = read_sync_progress(workspace_dir)
    assert read is not None
    return read


def progress(passes=None, **fields):
    return SyncProgress('main', '//ws', 'tip1', 'pre1', {'backend': 'git', 'branch': 'feature',
                                                         'on-sync-branch': False},
                        passes if passes is not None else [
                            ProgressPass(101, 'latest')],
                        written_by='0.14.0', started=STARTED, **fields)


class TestSyncProgressFile(WorkspaceTestCase):
    def test_what_is_written_reads_back_the_same(self):
        kept = progress(
            passes=[
                ProgressPass(100, 'last synced', lists=PassLists(
                    files=[('upd', 'Game/a.uasset'),
                           ('add', 'dir with space/"quoted".txt')],
                    changed=[LocalChange('Config/x.ini', 'base1'), LocalChange('new.txt', None, added_both=True),
                             LocalChange('art.psd', 'base2', binary=True)],
                    ignored=['Saved/x.log'], always_writable=['Saved/w.log'])),
                ProgressPass(102, 'cherry-picked', exact=True),
                ProgressPass(103, 'split', subject='git-p4son: p4 sync //ws/...@103')],
            changed=[LocalChange('b.txt', 'base3')], merged=[MergedFile('a.txt', 'added-both')],
            ignored=['Saved/y.log'], synced=['a.txt'])
        write_sync_progress(kept, self.ws)
        self.assertEqual(read_sync_progress(self.ws), kept)

    def test_only_the_end_left_reads_back_with_no_passes(self):
        write_sync_progress(progress(passes=[], changed=[
                            LocalChange('b.txt', 'base3')]), self.ws)
        self.assertEqual(written(self.ws).passes, [])

    def test_it_is_toml_with_a_line_per_list_item(self):
        text = format_sync_progress(progress(ignored=['x.log', 'y.log']))
        self.assertEqual(tomllib.loads(
            text)['end']['ignored'], ['x.log', 'y.log'])
        self.assertIn('ignored = [\n    "x.log",\n    "y.log",\n]\n', text)
        self.assertIn('started = 2026-11-25T09:12:44+00:00\n', text)

    def test_it_is_written_in_the_ignored_config_dir(self):
        write_sync_progress(progress(), self.ws)
        self.assertEqual(progress_path(self.ws), os.path.join(
            self.ws, '.git-p4son', 'sync-progress.toml'))
        with open(os.path.join(self.ws, '.git-p4son', '.gitignore')) as f:
            self.assertEqual(f.read(), '*\n')

    def test_a_failed_write_leaves_the_file_before_it(self):
        write_sync_progress(progress(), self.ws)
        with mock.patch('git_p4son.sync_progress.format_sync_progress', side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            write_sync_progress(progress(passes=[]), self.ws)
        self.assertEqual(written(self.ws).passes,
                         [ProgressPass(101, 'latest')])

    def test_a_replace_refused_while_another_process_has_the_file_open_is_retried(self):
        replace = os.replace
        attempts = []

        def refused_once(source, target):
            attempts.append(target)
            if len(attempts) == 1:
                raise PermissionError(13, 'Access is denied')
            replace(source, target)
        with mock.patch('git_p4son.sync_progress.os.replace', side_effect=refused_once), \
                mock.patch('git_p4son.sync_progress.time.sleep'):
            write_sync_progress(progress(), self.ws)
        self.assertEqual(len(attempts), 2)
        self.assertEqual(written(self.ws).passes, [
                         ProgressPass(101, 'latest')])

    def test_a_replace_refused_every_time_raises(self):
        with mock.patch('git_p4son.sync_progress.os.replace', side_effect=PermissionError(13, 'Access is denied')), \
                mock.patch('git_p4son.sync_progress.time.sleep'), self.assertRaises(PermissionError):
            write_sync_progress(progress(), self.ws)

    def test_none_without_a_file(self):
        self.assertIsNone(read_sync_progress(self.ws))

    def test_remove(self):
        write_sync_progress(progress(), self.ws)
        remove_sync_progress(self.ws)
        self.assertIsNone(read_sync_progress(self.ws))
        remove_sync_progress(self.ws)


class TestProgressRecorder(WorkspaceTestCase):
    def recorder(self, passes, writable_mode=False):
        return ProgressRecorder(progress(passes=passes), self.ws, writable_mode)

    def made(self, recorder, prep, tip=None):
        """Run the first pass left with prep, as sync would: its lists, then the pass made."""
        recorder.before_sync([], prep)
        recorder.pass_made(prep, tip)

    def path(self, name):
        return os.path.join(self.ws, *name.split('/'))

    def changed(self, name, base='base1'):
        return ChangedFile(self.path(name), base, None, None)

    def test_before_sync_records_the_first_pass_lists_with_paths_from_the_workspace_root(self):
        recorder = self.recorder(
            [ProgressPass(101, 'latest'), ProgressPass(102, 'latest')])
        prep = WritableSyncFileSet(changed=[self.changed('d/a.txt')], ignored=[self.path('x.log')],
                                   always_writable=[self.path('w.log')])
        recorder.before_sync([P4SyncPreviewFile('upd', self.path('d/a.txt')),
                              P4SyncPreviewFile('add', self.path('b.txt'))], prep)
        first, second = written(self.ws).passes
        self.assertEqual(first.lists, PassLists(
            files=[('upd', 'd/a.txt'), ('add', 'b.txt')], changed=[LocalChange('d/a.txt', 'base1')],
            ignored=['x.log'], always_writable=['w.log']))
        self.assertIsNone(second.lists)

    def test_pass_made_drops_it_and_keeps_what_the_end_needs(self):
        recorder = self.recorder(
            [ProgressPass(100, 'last synced'), ProgressPass(101, 'latest')])
        self.made(recorder, WritableSyncFileSet(changed=[self.changed('a.txt')], ignored=[self.path('x.log')],
                                                synced=[self.path('a.txt')]))
        result = written(self.ws)
        self.assertEqual([p.changelist for p in result.passes], [101])
        self.assertEqual((result.changed, result.ignored, result.synced, result.tip),
                         ([LocalChange('a.txt', 'base1')], ['x.log'], [], 'tip1'))

    def test_a_commit_moves_the_tip(self):
        recorder = self.recorder([ProgressPass(101, 'latest')])
        self.made(recorder, WritableSyncFileSet(), 'tip2')
        self.assertEqual(written(self.ws).tip, 'tip2')

    def test_a_file_in_several_passes_is_kept_once(self):
        recorder = self.recorder(
            [ProgressPass(100, 'last synced'), ProgressPass(101, 'latest')], True)
        for base in ('base1', 'base2'):
            self.made(recorder, WritableSyncFileSet(changed=[self.changed('a.txt', base)],
                                                    ignored=[self.path('x.log')], synced=[self.path('a.txt')]))
        result = written(self.ws)
        self.assertEqual((result.changed, result.ignored, result.synced),
                         ([LocalChange('a.txt', 'base2')], ['x.log'], ['a.txt']))

    def test_merged_drops_the_local_change_and_keeps_its_outcome(self):
        recorder = self.recorder([ProgressPass(101, 'latest')])
        self.made(recorder, WritableSyncFileSet(
            changed=[self.changed('a.txt'), self.changed('b.txt')]))
        recorder.merged(self.path('a.txt'), 'conflicts')
        result = written(self.ws)
        self.assertEqual((result.changed, result.merged),
                         ([LocalChange('b.txt', 'base1')], [MergedFile('a.txt', 'conflicts')]))

    def test_a_merge_with_nothing_to_report_keeps_no_outcome(self):
        recorder = self.recorder([ProgressPass(101, 'latest')])
        self.made(recorder, WritableSyncFileSet(
            changed=[self.changed('a.txt')]))
        recorder.merged(self.path('a.txt'), None)
        self.assertEqual(written(self.ws).merged, [])

    def test_no_progress_writes_nothing(self):
        recorder = NoProgress()
        recorder.before_sync([], WritableSyncFileSet())
        recorder.pass_made(WritableSyncFileSet(), 'tip2')
        recorder.merged(self.path('a.txt'), 'clean')
        recorder.finish()
        self.assertFalse(os.path.exists(os.path.join(self.ws, '.git-p4son')))

    def test_finish_removes_it(self):
        recorder = self.recorder([ProgressPass(101, 'latest')])
        self.made(recorder, WritableSyncFileSet())
        recorder.finish()
        self.assertIsNone(read_sync_progress(self.ws))


if __name__ == '__main__':
    unittest.main()
