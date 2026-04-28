"""Tests for git_p4son.sync module."""

import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

from git_p4son.common import CommandError, RunError
from git_p4son.perforce import (
    P4ClientSpec,
    P4SyncOutputProcessor,
    get_latest_changelist,
    get_writable_files,
    p4_get_opened_files,
    parse_p4_sync_line,
)
from git_p4son.git import (
    add_all_files,
    commit,
    get_dirty_files,
)
from git_p4son.sync import (
    LastSync,
    WritableSyncFileSet,
    git_last_sync,
    p4_sync,
    prepare_writable_files,
    sync_command,
)
from tests.helpers import make_run_result


class TestParseP4SyncLine(unittest.TestCase):
    def test_added_file(self):
        mode, filename = parse_p4_sync_line(
            '//depot/foo.txt#1 - added as /ws/foo.txt')
        self.assertEqual(mode, 'add')
        self.assertEqual(filename, '/ws/foo.txt')

    def test_deleted_file(self):
        mode, filename = parse_p4_sync_line(
            '//depot/foo.txt#2 - deleted as /ws/foo.txt')
        self.assertEqual(mode, 'del')
        self.assertEqual(filename, '/ws/foo.txt')

    def test_updated_file(self):
        mode, filename = parse_p4_sync_line(
            '//depot/foo.txt#3 - updating /ws/foo.txt')
        self.assertEqual(mode, 'upd')
        self.assertEqual(filename, '/ws/foo.txt')

    def test_clobber_file(self):
        mode, filename = parse_p4_sync_line(
            "Can't clobber writable file /ws/foo.txt")
        self.assertEqual(mode, 'clb')
        self.assertEqual(filename, '/ws/foo.txt')

    def test_unparsable_line(self):
        mode, filename = parse_p4_sync_line('some random output')
        self.assertIsNone(mode)
        self.assertIsNone(filename)


class TestGetWritableFiles(unittest.TestCase):
    def test_extracts_writable_files(self):
        stderr = [
            "Can't clobber writable file /ws/a.txt",
            "Can't clobber writable file /ws/b.txt",
            "some other error",
        ]
        result = get_writable_files(stderr)
        self.assertEqual(result, ['/ws/a.txt', '/ws/b.txt'])

    def test_empty_stderr(self):
        self.assertEqual(get_writable_files([]), [])


class TestGitGetDirtyFiles(unittest.TestCase):
    @mock.patch('git_p4son.git.run_with_output')
    def test_clean_workspace(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[])
        self.assertEqual(get_dirty_files('/ws'), [])

    @mock.patch('git_p4son.git.run_with_output')
    def test_modified_file(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[' M file.txt'])
        result = get_dirty_files('/ws')
        self.assertEqual(result, [('file.txt', 'modify')])

    @mock.patch('git_p4son.git.run_with_output')
    def test_added_file(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=['A  new.txt'])
        result = get_dirty_files('/ws')
        self.assertEqual(result, [('new.txt', 'add')])

    @mock.patch('git_p4son.git.run_with_output')
    def test_deleted_file(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[' D gone.txt'])
        result = get_dirty_files('/ws')
        self.assertEqual(result, [('gone.txt', 'delete')])

    @mock.patch('git_p4son.git.run_with_output')
    def test_untracked_file(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=['?? unknown.txt'])
        result = get_dirty_files('/ws')
        self.assertEqual(result, [('unknown.txt', 'untracked')])

    @mock.patch('git_p4son.git.run_with_output')
    def test_multiple_files(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            ' M mod.txt',
            'A  add.txt',
            '?? new.txt',
        ])
        result = get_dirty_files('/ws')
        self.assertEqual(result, [
            ('mod.txt', 'modify'),
            ('add.txt', 'add'),
            ('new.txt', 'untracked'),
        ])

    @mock.patch('git_p4son.git.run_with_output')
    def test_command_failure(self, mock_rwo):
        mock_rwo.side_effect = RunError('git status failed')
        with self.assertRaises(RunError):
            get_dirty_files('/ws')


class TestP4GetOpenedFiles(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run_with_output')
    def test_clean(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[])
        self.assertEqual(p4_get_opened_files('//depot', '/ws'), [])

    @mock.patch('git_p4son.perforce.run_with_output')
    def test_edit(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '... depotFile //depot/foo.txt',
            '... action edit',
            '... change default',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('//depot/foo.txt', 'modify')])

    @mock.patch('git_p4son.perforce.run_with_output')
    def test_add(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '... depotFile //depot/new.txt',
            '... action add',
            '... change 12345',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('//depot/new.txt', 'add')])

    @mock.patch('git_p4son.perforce.run_with_output')
    def test_delete(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '... depotFile //depot/old.txt',
            '... action delete',
            '... change 12345',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('//depot/old.txt', 'delete')])

    @mock.patch('git_p4son.perforce.run_with_output')
    def test_move_add(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '... depotFile //depot/new.txt',
            '... action move/add',
            '... change 12345',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('//depot/new.txt', 'add')])

    @mock.patch('git_p4son.perforce.run_with_output')
    def test_multiple_files(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '... depotFile //depot/a.txt',
            '... action edit',
            '... change default',
            '',
            '... depotFile //depot/b.txt',
            '... action add',
            '... change 12345',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [
            ('//depot/a.txt', 'modify'),
            ('//depot/b.txt', 'add'),
        ])

    @mock.patch('git_p4son.perforce.run_with_output')
    def test_command_failure(self, mock_rwo):
        mock_rwo.side_effect = RunError('p4 opened failed')
        with self.assertRaises(RunError):
            p4_get_opened_files('//depot', '/ws')


class TestGitAddAllFiles(unittest.TestCase):
    @mock.patch('git_p4son.git.run_with_output')
    def test_success(self, mock_rwo):
        mock_rwo.return_value = make_run_result()
        add_all_files('/ws')

    @mock.patch('git_p4son.git.run_with_output')
    def test_failure(self, mock_rwo):
        mock_rwo.side_effect = RunError('git add failed')
        with self.assertRaises(RunError):
            add_all_files('/ws')


class TestGitCommit(unittest.TestCase):
    @mock.patch('git_p4son.git.run_with_output')
    def test_success(self, mock_rwo):
        mock_rwo.return_value = make_run_result()
        commit('msg', '/ws')
        cmd = mock_rwo.call_args[0][0]
        self.assertEqual(cmd, ['git', 'commit', '-m', 'msg'])

    @mock.patch('git_p4son.git.run_with_output')
    def test_allow_empty(self, mock_rwo):
        mock_rwo.return_value = make_run_result()
        commit('msg', '/ws', allow_empty=True)
        cmd = mock_rwo.call_args[0][0]
        self.assertIn('--allow-empty', cmd)


class TestGitLastSync(unittest.TestCase):
    HASH = 'abc123def456' * 3 + 'abcd'  # 40-char fake hash

    @mock.patch('git_p4son.sync.run_with_output')
    def test_extracts_changelist_and_commit(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            f'{self.HASH} 12345: p4 sync //...@12345'
        ])
        result = git_last_sync('/ws')
        self.assertEqual(result.changelist, 12345)
        self.assertEqual(result.commit, self.HASH)

    @mock.patch('git_p4son.sync.run_with_output')
    def test_extracts_changelist_pergit(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            f'{self.HASH} pergit: p4 sync //...@12345'
        ])
        result = git_last_sync('/ws')
        self.assertEqual(result.changelist, 12345)

    @mock.patch('git_p4son.sync.run_with_output')
    def test_extracts_changelist_git_p4son(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            f'{self.HASH} git-p4son: p4 sync //...@12345'
        ])
        result = git_last_sync('/ws')
        self.assertEqual(result.changelist, 12345)

    @mock.patch('git_p4son.sync.run_with_output')
    def test_extracts_changelist_fail(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            f'{self.HASH} pergot: p4 sync //...@12345'
        ])
        result = git_last_sync('/ws')
        self.assertIsNone(result)

    @mock.patch('git_p4son.sync.run_with_output')
    def test_no_match(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            f'{self.HASH} "some other commit message"'
        ])
        result = git_last_sync('/ws')
        self.assertIsNone(result)

    @mock.patch('git_p4son.sync.run_with_output')
    def test_empty_output(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[])
        result = git_last_sync('/ws')
        self.assertIsNone(result)

    @mock.patch('git_p4son.sync.run_with_output')
    def test_command_failure(self, mock_rwo):
        mock_rwo.side_effect = RunError('git log failed')
        with self.assertRaises(RunError):
            git_last_sync('/ws')

    @mock.patch('git_p4son.sync.run_with_output')
    def test_new_format_with_depot_root(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            f'{self.HASH} git-p4son: p4 sync //my-client/Engine/Source/...@12345'
        ])
        result = git_last_sync('/ws')
        self.assertEqual(result.changelist, 12345)

    @mock.patch('git_p4son.sync.run_with_output')
    def test_uses_git_grep_to_search_history(self, mock_rwo):
        """Verifies git log --grep is used so sync commits are found
        even when HEAD is not a sync commit."""
        mock_rwo.return_value = make_run_result(stdout=[
            f'{self.HASH} git-p4son: p4 sync //...@99999'
        ])
        result = git_last_sync('/ws')
        self.assertEqual(result.changelist, 99999)
        cmd = mock_rwo.call_args[0][0]
        self.assertIn('--grep=: p4 sync //', cmd)


class TestGetLatestChangelist(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_success(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... change 54321',
            '... time 1704067200',
            '... user user',
            '... client ws',
        ])
        cl = get_latest_changelist('//myclient', '/ws')
        self.assertEqual(cl, 54321)
        mock_run.assert_called_once_with(
            ['p4', '-ztag', 'changes', '-m1', '-s', 'submitted',
             '//myclient/...#head'], cwd='/ws')

    @mock.patch('git_p4son.perforce.run')
    def test_no_changes_found(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        with self.assertRaises(CommandError):
            get_latest_changelist('//myclient', '/ws')


class TestP4SyncOutputProcessor(unittest.TestCase):
    def test_tracks_added_file(self):
        processor = P4SyncOutputProcessor()
        processor('//depot/foo.txt#1 - added as /ws/foo.txt', sys.stdout)
        self.assertEqual(processor.stats['add'], 1)
        self.assertEqual(processor.synced_file_count, 1)

    def test_tracks_deleted_file(self):
        processor = P4SyncOutputProcessor()
        processor('//depot/foo.txt#2 - deleted as /ws/foo.txt', sys.stdout)
        self.assertEqual(processor.stats['del'], 1)

    def test_up_to_date_message(self):
        processor = P4SyncOutputProcessor()
        processor('//...@12345 - file(s) up-to-date.', sys.stdout)
        self.assertEqual(processor.synced_file_count, 0)


class TestP4Sync(unittest.TestCase):
    @mock.patch('git_p4son.sync.run_with_output')
    def test_success(self, mock_rwo):
        mock_rwo.return_value = make_run_result()
        p4_sync(12345, 'test', '//myclient', '/ws')

    @mock.patch('git_p4son.sync.run_with_output')
    def test_expected_clobber_tolerated(self, mock_rwo):
        mock_rwo.side_effect = RunError(
            'p4 sync failed', returncode=1,
            stderr=["Can't clobber writable file /ws/a.txt",
                    "Can't clobber writable file /ws/b.txt"])
        p4_sync(12345, 'test', '//myclient', '/ws',
                expected_clobber={'/ws/a.txt', '/ws/b.txt'})

    @mock.patch('git_p4son.sync.run_with_output')
    def test_unexpected_clobber_raises(self, mock_rwo):
        mock_rwo.side_effect = RunError(
            'p4 sync failed', returncode=1,
            stderr=["Can't clobber writable file /ws/a.txt"])
        with self.assertRaises(RunError):
            p4_sync(12345, 'test', '//myclient', '/ws',
                    expected_clobber=set())

    @mock.patch('git_p4son.sync.run_with_output')
    def test_non_clobber_error_raises(self, mock_rwo):
        mock_rwo.side_effect = RunError(
            'p4 sync failed', returncode=1,
            stderr=['some other error'])
        with self.assertRaises(RunError):
            p4_sync(12345, 'test', '//myclient', '/ws')


class TestPrepareWritableFiles(unittest.TestCase):
    def _make_file(self, ws, name, content='content', writable=True):
        path = os.path.join(ws, name)
        with open(path, 'w') as f:
            f.write(content)
        if writable:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        else:
            os.chmod(path, stat.S_IRUSR)
        return path

    @mock.patch('git_p4son.sync.p4_fstat_file_info')
    @mock.patch('git_p4son.sync.get_ignored_files', return_value=set())
    def test_changed_file_added_to_changed(self, _ignored, mock_fstat):
        from git_p4son.perforce import P4FileInfo
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {
                path: P4FileInfo(head_type='text', digest='DIFFERENT')}
            result = prepare_writable_files([path], ws)

            self.assertEqual(result.changed, [path])
            self.assertEqual(result.ignored, [])
            mode = os.stat(path).st_mode
            self.assertFalse(mode & stat.S_IWUSR)

    @mock.patch('git_p4son.sync.compute_local_md5', return_value='AAAA')
    @mock.patch('git_p4son.sync.p4_fstat_file_info')
    @mock.patch('git_p4son.sync.get_ignored_files', return_value=set())
    def test_unchanged_file_not_in_changed(self, _ignored, mock_fstat, _md5):
        from git_p4son.perforce import P4FileInfo
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {
                path: P4FileInfo(head_type='text', digest='AAAA')}
            result = prepare_writable_files([path], ws)

            self.assertEqual(result.changed, [])
            mode = os.stat(path).st_mode
            self.assertFalse(mode & stat.S_IWUSR)

    @mock.patch('git_p4son.sync.p4_fstat_file_info')
    @mock.patch('git_p4son.sync.get_ignored_files', return_value=set())
    def test_binary_files_tracked(self, _ignored, mock_fstat):
        from git_p4son.perforce import P4FileInfo
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'image.png')
            mock_fstat.return_value = {
                path: P4FileInfo(head_type='binary', digest=None)}
            result = prepare_writable_files([path], ws)

            self.assertEqual(result.changed, [path])
            self.assertIn(path, result.binary)

    def test_readonly_files_skipped(self):
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt', writable=False)
            result = prepare_writable_files([path], ws)

            self.assertEqual(result.changed, [])
            self.assertEqual(result.ignored, [])

    def test_git_ignored_files_not_made_readonly(self):
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'build.log')

            with mock.patch('git_p4son.sync.get_ignored_files',
                            return_value={path}):
                result = prepare_writable_files([path], ws)

            self.assertEqual(result.changed, [])
            self.assertEqual(result.ignored, [path])
            mode = os.stat(path).st_mode
            self.assertTrue(mode & stat.S_IWUSR)

    def test_nonexistent_files_skipped(self):
        result = prepare_writable_files(['/ws/noexist.txt'], '/ws')
        self.assertEqual(result.changed, [])
        self.assertEqual(result.ignored, [])

    def test_empty_input(self):
        result = prepare_writable_files([], '/ws')
        self.assertEqual(result.changed, [])
        self.assertEqual(result.ignored, [])


_MOCK_SPEC = P4ClientSpec(
    name='my-ws', root='/ws',
    options=['noallwrite', 'clobber', 'nocompress'], stream=None,
    line_end='local')


@mock.patch('git_p4son.sync.get_client_spec', return_value=_MOCK_SPEC)
class TestSyncCommand(unittest.TestCase):

    _last_sync = LastSync(changelist=10000, commit='abc123')

    def _empty_prep(self):
        return WritableSyncFileSet()

    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.get_dirty_files')
    @mock.patch('git_p4son.sync.p4_sync')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview', return_value=[])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_sync')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_sync_specific_cl(self, _depot, _p4clean, mock_last_sync,
                              _head, _preview, mock_prep, _p4sync,
                              mock_git_clean, _git_add, _git_commit,
                              _spec):
        mock_last_sync.return_value = self._last_sync
        mock_prep.return_value = self._empty_prep()
        mock_git_clean.side_effect = [[], [('file.txt', 'modify')]]
        args = mock.Mock(changelist='12345', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.sync.get_depot_root', return_value=None)
    def test_no_depot_root_aborts(self, _depot, _spec):
        args = mock.Mock(changelist='100', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.get_dirty_files',
                return_value=[('file.txt', 'modify')])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_dirty_git_workspace_aborts(self, _depot, _git_clean, _spec):
        args = mock.Mock(changelist='100', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.p4_get_opened_files',
                return_value=[('//depot/foo.txt', 'modify')])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_dirty_p4_workspace_aborts(self, _depot, _p4clean, _git_clean,
                                       _spec):
        args = mock.Mock(changelist='100', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.git_last_sync')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_older_cl_without_force_aborts(self, _depot, _git_clean,
                                           _p4clean, mock_last_sync,
                                           _spec):
        mock_last_sync.return_value = LastSync(changelist=200, commit='abc')
        args = mock.Mock(changelist='100', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.get_dirty_files')
    @mock.patch('git_p4son.sync.p4_sync')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview', return_value=[])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_sync')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_older_cl_with_force_proceeds(self, _depot, _p4clean,
                                          mock_last_sync, _head, _preview,
                                          mock_prep, _p4sync,
                                          mock_git_clean, _add, _commit,
                                          _spec):
        mock_last_sync.return_value = LastSync(changelist=200, commit='abc')
        mock_prep.return_value = self._empty_prep()
        mock_git_clean.side_effect = [[], [('file.txt', 'modify')]]
        args = mock.Mock(changelist='100', force=True, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.sync.p4_sync_preview', return_value=[])
    @mock.patch('git_p4son.sync.git_last_sync')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_same_cl_is_noop(self, _depot, _git_clean, _p4clean,
                             mock_last_sync, _preview, _spec):
        mock_last_sync.return_value = LastSync(changelist=100, commit='abc')
        args = mock.Mock(changelist='100', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.sync.p4_sync')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview', return_value=['/ws/a.txt'])
    @mock.patch('git_p4son.sync.git_last_sync')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_last_synced(self, _depot, _git_clean, _p4clean,
                         mock_last_sync, _preview, mock_prep,
                         mock_p4sync, _spec):
        mock_last_sync.return_value = LastSync(changelist=100, commit='abc')
        mock_prep.return_value = self._empty_prep()
        args = mock.Mock(changelist='last-synced',
                         force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        mock_p4sync.assert_called_once_with(
            100, 'last synced', '//myclient', '/ws',
            expected_clobber=set())

    @mock.patch('git_p4son.sync.p4_sync')
    @mock.patch('git_p4son.sync.p4_sync_preview', return_value=[])
    @mock.patch('git_p4son.sync.git_last_sync')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_last_synced_skips_sync_when_preview_empty(
            self, _depot, _git_clean, _p4clean, mock_last_sync,
            _preview, mock_p4sync, _spec):
        mock_last_sync.return_value = LastSync(changelist=100, commit='abc')
        args = mock.Mock(changelist='last-synced',
                         force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        mock_p4sync.assert_not_called()

    @mock.patch('git_p4son.sync.get_latest_changelist')
    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.get_dirty_files')
    @mock.patch('git_p4son.sync.p4_sync')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview', return_value=[])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_sync')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_latest_keyword(self, _depot, _p4clean, mock_last_sync, _head,
                            _preview, mock_prep, _p4sync,
                            mock_git_clean, _commit, mock_get_latest,
                            _spec):
        mock_last_sync.return_value = LastSync(changelist=100, commit='abc')
        mock_get_latest.return_value = 200
        mock_prep.return_value = self._empty_prep()
        mock_git_clean.side_effect = [[], []]  # clean before and after
        args = mock.Mock(changelist=None, force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)


class TestMergeChangedFiles(unittest.TestCase):
    """Tests for _merge_changed_files."""

    @mock.patch('git_p4son.sync._make_writable')
    @mock.patch('git_p4son.sync.merge_file')
    @mock.patch('git_p4son.sync.get_file_at_commit')
    def test_absolute_paths_converted_to_relative(self, mock_get_file,
                                                  mock_merge, _writable):
        """Files from clobber errors use absolute paths. _merge_changed_files
        must convert them to repo-relative paths for git show."""
        workspace = '/ws'
        abs_path = '/ws/src/test.cpp'
        user_content = b'local version'
        p4_content = b'perforce version'

        # Track what paths get_file_at_commit is called with
        def fake_get_file(filepath, commit, ws):
            if filepath == 'src/test.cpp':
                return user_content
            # If called with absolute path, return None (simulates the bug)
            return None

        mock_get_file.side_effect = fake_get_file
        mock_merge.return_value = (True, b'merged content')

        with mock.patch('builtins.open', mock.mock_open(read_data=p4_content)):
            with mock.patch('os.path.exists', return_value=True):
                from git_p4son.sync import _merge_changed_files
                _merge_changed_files(
                    [abs_path], 'user123', 'sync456', workspace)

        # Verify get_file_at_commit was called with relative path
        calls = mock_get_file.call_args_list
        self.assertEqual(len(calls), 2)
        # ours
        self.assertEqual(calls[0][0][0], 'src/test.cpp')
        # base
        self.assertEqual(calls[1][0][0], 'src/test.cpp')

        # Verify merge was actually called (not short-circuited by the bug)
        mock_merge.assert_called_once()

    @mock.patch('git_p4son.sync.merge_file')
    @mock.patch('git_p4son.sync.get_file_at_commit')
    def test_writes_to_readonly_file_after_force_sync(self, mock_get_file,
                                                      mock_merge):
        """After p4 sync -f, files are read-only. _merge_changed_files must
        handle writing merged content to read-only files."""
        with tempfile.TemporaryDirectory() as workspace:
            # Create a read-only file (simulating post p4 sync -f state)
            filepath = os.path.join(workspace, 'test.cpp')
            with open(filepath, 'wb') as f:
                f.write(b'perforce version')
            os.chmod(filepath, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

            mock_get_file.return_value = b'local version'
            mock_merge.return_value = (True, b'merged content')

            from git_p4son.sync import _merge_changed_files
            _merge_changed_files(
                [filepath], 'user123', 'sync456', workspace)

            # Verify merged content was written despite read-only flag
            with open(filepath, 'rb') as f:
                self.assertEqual(f.read(), b'merged content')

            mock_merge.assert_called_once()

    @mock.patch('git_p4son.sync.merge_file')
    @mock.patch('git_p4son.sync.get_file_at_commit')
    def test_binary_file_restores_user_version(self, mock_get_file,
                                               mock_merge):
        """Binary files (identified by Perforce type) should restore the
        user's version instead of attempting a three-way merge."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath = os.path.join(workspace, 'image.png')
            with open(filepath, 'wb') as f:
                f.write(b'perforce binary content')

            mock_get_file.return_value = b'user binary content'

            from git_p4son.sync import _merge_changed_files
            _merge_changed_files(
                [filepath], 'user123', 'sync456', workspace,
                binary_files={filepath})

            with open(filepath, 'rb') as f:
                self.assertEqual(f.read(), b'user binary content')

            mock_merge.assert_not_called()

    @mock.patch('git_p4son.sync.get_file_at_commit')
    def test_deleted_upstream_restores_changed_local_version(self,
                                                             mock_get_file):
        """When p4 deletes a file but the user has local changes, the user's
        version should be restored to disk as an untracked file."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath = os.path.join(workspace, 'test.cpp')

            # File doesn't exist on disk (p4 sync deleted it)
            # User modified the file (ours != base)
            def fake_get_file(path, commit, ws):
                if commit == 'user123':
                    return b'modified locally'
                return b'original from sync'

            mock_get_file.side_effect = fake_get_file

            from git_p4son.sync import _merge_changed_files
            _merge_changed_files(
                [filepath], 'user123', 'sync456', workspace)

            # User's version should be restored to disk
            with open(filepath, 'rb') as f:
                self.assertEqual(f.read(), b'modified locally')

    @mock.patch('git_p4son.sync.get_file_at_commit')
    def test_deleted_upstream_unchanged_local_not_restored(self,
                                                           mock_get_file):
        """When p4 deletes a file and the local version is unchanged from
        the last sync, the delete should stand (file not restored)."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath = os.path.join(workspace, 'test.cpp')
            content = b'unchanged content'

            # ours == base: file unchanged, just read-only flag cleared
            def fake_get_file(path, commit, ws):
                return content

            mock_get_file.side_effect = fake_get_file

            from git_p4son.sync import _merge_changed_files
            _merge_changed_files(
                [filepath], 'user123', 'sync456', workspace)

            # File should NOT be restored - upstream delete stands
            self.assertFalse(os.path.exists(filepath))


if __name__ == '__main__':
    unittest.main()
