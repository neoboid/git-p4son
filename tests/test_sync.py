"""Tests for git_p4son.sync and git_p4son.perforce modules."""

import sys
import unittest
from unittest import mock

from git_p4son.common import CommandError, RunError
from git_p4son.perforce import (
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
    git_changelist_of_last_sync,
    p4_sync,
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


class TestGitChangelistOfLastSync(unittest.TestCase):
    @mock.patch('git_p4son.sync.run_with_output')
    def test_extracts_changelist_nr(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '12345: p4 sync //...@12345'
        ])
        result = git_changelist_of_last_sync('/ws')
        self.assertEqual(result, 12345)

    @mock.patch('git_p4son.sync.run_with_output')
    def test_extracts_changelist_pergit(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            'pergit: p4 sync //...@12345'
        ])
        result = git_changelist_of_last_sync('/ws')
        self.assertEqual(result, 12345)

    @mock.patch('git_p4son.sync.run_with_output')
    def test_extracts_changelist_git_p4son(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            'git-p4son: p4 sync //...@12345'
        ])
        result = git_changelist_of_last_sync('/ws')
        self.assertEqual(result, 12345)

    @mock.patch('git_p4son.sync.run_with_output')
    def test_extracts_changelist_fail(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            'pergot: p4 sync //...@12345'
        ])
        result = git_changelist_of_last_sync('/ws')
        self.assertEqual(result, None)

    @mock.patch('git_p4son.sync.run_with_output')
    def test_no_match(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '"some other commit message"'
        ])
        result = git_changelist_of_last_sync('/ws')
        self.assertIsNone(result)

    @mock.patch('git_p4son.sync.run_with_output')
    def test_empty_output(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[])
        result = git_changelist_of_last_sync('/ws')
        self.assertIsNone(result)

    @mock.patch('git_p4son.sync.run_with_output')
    def test_command_failure(self, mock_rwo):
        mock_rwo.side_effect = RunError('git log failed')
        with self.assertRaises(RunError):
            git_changelist_of_last_sync('/ws')

    @mock.patch('git_p4son.sync.run_with_output')
    def test_new_format_with_depot_root(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            'git-p4son: p4 sync //my-client/Engine/Source/...@12345'
        ])
        result = git_changelist_of_last_sync('/ws')
        self.assertEqual(result, 12345)

    @mock.patch('git_p4son.sync.run_with_output')
    def test_uses_git_grep_to_search_history(self, mock_rwo):
        """Verifies git log --grep is used so sync commits are found
        even when HEAD is not a sync commit."""
        mock_rwo.return_value = make_run_result(stdout=[
            'git-p4son: p4 sync //...@99999'
        ])
        result = git_changelist_of_last_sync('/ws')
        self.assertEqual(result, 99999)
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
        result = p4_sync(12345, 'test', False, '//myclient', '/ws')
        self.assertTrue(result)


class TestSyncCommand(unittest.TestCase):
    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.get_dirty_files')
    @mock.patch('git_p4son.sync.p4_sync', return_value=True)
    @mock.patch('git_p4son.sync.git_changelist_of_last_sync', return_value=10000)
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_sync_specific_cl(self, _depot, _p4clean, _last_cl, _p4sync,
                              mock_git_clean, _git_add, _git_commit):
        # First call: initial check (clean), second call: after sync (dirty -> add files)
        mock_git_clean.side_effect = [[], [('file.txt', 'modify')]]
        args = mock.Mock(changelist='12345', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.sync.get_depot_root', return_value=None)
    def test_no_depot_root_aborts(self, _depot):
        args = mock.Mock(changelist='100', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.get_dirty_files',
                return_value=[('file.txt', 'modify')])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_dirty_git_workspace_aborts(self, _depot, _git_clean):
        args = mock.Mock(changelist='100', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.p4_get_opened_files',
                return_value=[('//depot/foo.txt', 'modify')])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_dirty_p4_workspace_aborts(self, _depot, _p4clean, _git_clean):
        args = mock.Mock(changelist='100', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.git_changelist_of_last_sync', return_value=200)
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_older_cl_without_force_aborts(self, _depot, _git_clean,
                                           _p4clean, _last_cl):
        args = mock.Mock(changelist='100', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.get_dirty_files')
    @mock.patch('git_p4son.sync.p4_sync', return_value=True)
    @mock.patch('git_p4son.sync.git_changelist_of_last_sync', return_value=200)
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_older_cl_with_force_proceeds(self, _depot, _p4clean, _last_cl,
                                          _p4sync, mock_git_clean,
                                          _add, _commit):
        mock_git_clean.side_effect = [[], [('file.txt', 'modify')]]
        args = mock.Mock(changelist='100', force=True, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.sync.p4_sync', return_value=True)
    @mock.patch('git_p4son.sync.git_changelist_of_last_sync', return_value=100)
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_same_cl_is_noop(self, _depot, _git_clean, _p4clean,
                             _last_cl, _p4sync):
        args = mock.Mock(changelist='100', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.sync.p4_sync', return_value=True)
    @mock.patch('git_p4son.sync.git_changelist_of_last_sync', return_value=100)
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_last_synced(self, _depot, _git_clean, _p4clean,
                         _last_cl, mock_p4sync):
        args = mock.Mock(changelist='last-synced',
                         force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        mock_p4sync.assert_called_once_with(
            100, 'last synced', False, '//myclient', '/ws')

    @mock.patch('git_p4son.sync.get_latest_changelist')
    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.get_dirty_files')
    @mock.patch('git_p4son.sync.p4_sync', return_value=True)
    @mock.patch('git_p4son.sync.git_changelist_of_last_sync', return_value=100)
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_latest_keyword(self, _depot, _p4clean, _last_cl, _p4sync,
                            mock_git_clean, _commit, mock_get_latest):
        mock_get_latest.return_value = 200
        mock_git_clean.side_effect = [[], []]  # clean before and after
        args = mock.Mock(changelist=None, force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)


if __name__ == '__main__':
    unittest.main()
