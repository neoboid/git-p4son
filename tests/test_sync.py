"""Tests for git_p4son.sync module."""

import sys
import unittest
from unittest import mock

from git_p4son.common import CommandError, RunError
from git_p4son.sync import (
    P4SyncOutputProcessor,
    echo_output_to_stream,
    get_file_count_to_sync,
    get_latest_changelist_affecting_workspace,
    get_writable_files,
    git_add_all_files,
    git_changelist_of_last_sync,
    git_commit,
    git_is_workspace_clean,
    p4_is_workspace_clean,
    p4_sync,
    parse_p4_sync_line,
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


class TestGitIsWorkspaceClean(unittest.TestCase):
    @mock.patch('git_p4son.sync.run_with_output')
    def test_clean_workspace(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[])
        self.assertTrue(git_is_workspace_clean('/ws'))

    @mock.patch('git_p4son.sync.run_with_output')
    def test_dirty_workspace(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=['M file.txt'])
        self.assertFalse(git_is_workspace_clean('/ws'))

    @mock.patch('git_p4son.sync.run_with_output')
    def test_command_failure(self, mock_rwo):
        mock_rwo.side_effect = RunError('git status failed')
        with self.assertRaises(RunError):
            git_is_workspace_clean('/ws')


class TestP4IsWorkspaceClean(unittest.TestCase):
    @mock.patch('git_p4son.sync.run_with_output')
    def test_clean(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[])
        self.assertTrue(p4_is_workspace_clean('/ws'))

    @mock.patch('git_p4son.sync.run_with_output')
    def test_dirty(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '//depot/foo.txt#1 - edit default change (text)'
        ])
        self.assertFalse(p4_is_workspace_clean('/ws'))

    @mock.patch('git_p4son.sync.run_with_output')
    def test_command_failure(self, mock_rwo):
        mock_rwo.side_effect = RunError('p4 opened failed')
        with self.assertRaises(RunError):
            p4_is_workspace_clean('/ws')


class TestGitAddAllFiles(unittest.TestCase):
    @mock.patch('git_p4son.sync.run_with_output')
    def test_success(self, mock_rwo):
        mock_rwo.return_value = make_run_result()
        git_add_all_files('/ws')

    @mock.patch('git_p4son.sync.run_with_output')
    def test_failure(self, mock_rwo):
        mock_rwo.side_effect = RunError('git add failed')
        with self.assertRaises(RunError):
            git_add_all_files('/ws')


class TestGitCommit(unittest.TestCase):
    @mock.patch('git_p4son.sync.run_with_output')
    def test_success(self, mock_rwo):
        mock_rwo.return_value = make_run_result()
        git_commit('msg', '/ws')
        cmd = mock_rwo.call_args[0][0]
        self.assertEqual(cmd, ['git', 'commit', '-m', 'msg'])

    @mock.patch('git_p4son.sync.run_with_output')
    def test_allow_empty(self, mock_rwo):
        mock_rwo.return_value = make_run_result()
        git_commit('msg', '/ws', allow_empty=True)
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
    def test_uses_git_grep_to_search_history(self, mock_rwo):
        """Verifies git log --grep is used so sync commits are found
        even when HEAD is not a sync commit."""
        mock_rwo.return_value = make_run_result(stdout=[
            'git-p4son: p4 sync //...@99999'
        ])
        result = git_changelist_of_last_sync('/ws')
        self.assertEqual(result, 99999)
        cmd = mock_rwo.call_args[0][0]
        self.assertIn('--grep=: p4 sync //\\.\\.\\.@', cmd)


class TestGetLatestChangelistAffectingWorkspace(unittest.TestCase):
    @mock.patch('git_p4son.sync.run')
    def test_success(self, mock_run):
        mock_run.side_effect = [
            make_run_result(stdout=['Client name: myclient', 'other: info']),
            make_run_result(stdout=[
                "Change 54321 on 2024/01/01 by user@ws 'description'"
            ]),
        ]
        cl = get_latest_changelist_affecting_workspace('/ws')
        self.assertEqual(cl, 54321)

    @mock.patch('git_p4son.sync.run')
    def test_p4_info_failure(self, mock_run):
        mock_run.side_effect = RunError('p4 info failed')
        with self.assertRaises(RunError):
            get_latest_changelist_affecting_workspace('/ws')

    @mock.patch('git_p4son.sync.run')
    def test_no_client_name(self, mock_run):
        mock_run.side_effect = [
            make_run_result(stdout=['Server: perforce:1666']),
        ]
        with self.assertRaises(CommandError):
            get_latest_changelist_affecting_workspace('/ws')

    @mock.patch('git_p4son.sync.run')
    def test_no_changes_found(self, mock_run):
        mock_run.side_effect = [
            make_run_result(stdout=['Client name: myclient']),
            make_run_result(stdout=[]),
        ]
        with self.assertRaises(CommandError):
            get_latest_changelist_affecting_workspace('/ws')


class TestGetFileCountToSync(unittest.TestCase):
    @mock.patch('git_p4son.sync.run')
    def test_returns_count(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '//depot/a.txt - added',
            '//depot/b.txt - updating',
        ])
        count = get_file_count_to_sync(12345, '/ws')
        self.assertEqual(count, 2)

    @mock.patch('git_p4son.sync.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = RunError('p4 sync -n failed')
        with self.assertRaises(RunError):
            get_file_count_to_sync(12345, '/ws')


class TestP4SyncOutputProcessor(unittest.TestCase):
    def test_tracks_added_file(self):
        processor = P4SyncOutputProcessor(10)
        processor('//depot/foo.txt#1 - added as /ws/foo.txt', sys.stdout)
        self.assertEqual(processor.stats['add'].count, 1)
        self.assertEqual(processor.synced_file_count, 1)

    def test_tracks_deleted_file(self):
        processor = P4SyncOutputProcessor(10)
        processor('//depot/foo.txt#2 - deleted as /ws/foo.txt', sys.stdout)
        self.assertEqual(processor.stats['del'].count, 1)

    def test_up_to_date_message(self):
        processor = P4SyncOutputProcessor(10)
        processor('//...@12345 - file(s) up-to-date.', sys.stdout)
        self.assertEqual(processor.synced_file_count, 0)


class TestP4Sync(unittest.TestCase):
    @mock.patch('git_p4son.sync.run_with_output')
    @mock.patch('git_p4son.sync.run')
    def test_success(self, mock_run, mock_rwo):
        mock_run.return_value = make_run_result(stdout=['file1', 'file2'])
        mock_rwo.return_value = make_run_result()
        result = p4_sync(12345, False, '/ws')
        self.assertTrue(result)

    @mock.patch('git_p4son.sync.run')
    def test_up_to_date(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        result = p4_sync(12345, False, '/ws')
        self.assertTrue(result)

    @mock.patch('git_p4son.sync.run')
    def test_count_failure(self, mock_run):
        mock_run.side_effect = RunError('p4 sync -n failed')
        with self.assertRaises(RunError):
            p4_sync(12345, False, '/ws')


class TestSyncCommand(unittest.TestCase):
    @mock.patch('git_p4son.sync.git_commit')
    @mock.patch('git_p4son.sync.git_add_all_files')
    @mock.patch('git_p4son.sync.git_is_workspace_clean')
    @mock.patch('git_p4son.sync.resolve_changelist', return_value='12345')
    @mock.patch('git_p4son.sync.p4_sync', return_value=True)
    @mock.patch('git_p4son.sync.git_changelist_of_last_sync', return_value=10000)
    @mock.patch('git_p4son.sync.p4_is_workspace_clean', return_value=True)
    def test_sync_specific_cl(self, _p4clean, _last_cl, _p4sync,
                              _resolve, mock_git_clean, _git_add, _git_commit):
        # First call: initial check (clean), second call: after sync (dirty -> add files)
        mock_git_clean.side_effect = [True, False]
        args = mock.Mock(changelist='12345', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.sync.git_is_workspace_clean', return_value=False)
    def test_dirty_git_workspace_aborts(self, _git_clean):
        args = mock.Mock(changelist='100', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.resolve_changelist', return_value='100')
    @mock.patch('git_p4son.sync.git_is_workspace_clean', return_value=True)
    @mock.patch('git_p4son.sync.p4_is_workspace_clean', return_value=False)
    def test_dirty_p4_workspace_aborts(self, _p4clean, _git_clean, _resolve):
        args = mock.Mock(changelist='100', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.resolve_changelist', return_value='100')
    @mock.patch('git_p4son.sync.git_changelist_of_last_sync', return_value=200)
    @mock.patch('git_p4son.sync.p4_is_workspace_clean', return_value=True)
    @mock.patch('git_p4son.sync.git_is_workspace_clean', return_value=True)
    def test_older_cl_without_force_aborts(self, _git_clean, _p4clean, _last_cl, _resolve):
        args = mock.Mock(changelist='100', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.resolve_changelist', return_value='100')
    @mock.patch('git_p4son.sync.git_commit')
    @mock.patch('git_p4son.sync.git_add_all_files')
    @mock.patch('git_p4son.sync.git_is_workspace_clean')
    @mock.patch('git_p4son.sync.p4_sync', return_value=True)
    @mock.patch('git_p4son.sync.git_changelist_of_last_sync', return_value=200)
    @mock.patch('git_p4son.sync.p4_is_workspace_clean', return_value=True)
    def test_older_cl_with_force_proceeds(self, _p4clean, _last_cl,
                                          _p4sync, mock_git_clean, _add, _commit, _resolve):
        mock_git_clean.side_effect = [True, False]
        args = mock.Mock(changelist='100', force=True, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.sync.resolve_changelist', return_value='100')
    @mock.patch('git_p4son.sync.p4_sync', return_value=True)
    @mock.patch('git_p4son.sync.git_changelist_of_last_sync', return_value=100)
    @mock.patch('git_p4son.sync.p4_is_workspace_clean', return_value=True)
    @mock.patch('git_p4son.sync.git_is_workspace_clean', return_value=True)
    def test_same_cl_is_noop(self, _git_clean, _p4clean, _last_cl, _p4sync, _resolve):
        args = mock.Mock(changelist='100', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.sync.p4_sync', return_value=True)
    @mock.patch('git_p4son.sync.git_changelist_of_last_sync', return_value=100)
    @mock.patch('git_p4son.sync.p4_is_workspace_clean', return_value=True)
    @mock.patch('git_p4son.sync.git_is_workspace_clean', return_value=True)
    def test_last_synced(self, _git_clean, _p4clean, _last_cl, mock_p4sync):
        args = mock.Mock(changelist='last-synced', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        mock_p4sync.assert_called_once_with(100, False, '/ws')

    @mock.patch('git_p4son.sync.get_latest_changelist_affecting_workspace')
    @mock.patch('git_p4son.sync.git_commit')
    @mock.patch('git_p4son.sync.git_is_workspace_clean')
    @mock.patch('git_p4son.sync.p4_sync', return_value=True)
    @mock.patch('git_p4son.sync.git_changelist_of_last_sync', return_value=100)
    @mock.patch('git_p4son.sync.p4_is_workspace_clean', return_value=True)
    def test_latest_keyword(self, _p4clean, _last_cl, _p4sync,
                            mock_git_clean, _commit, mock_get_latest):
        mock_get_latest.return_value = 200
        mock_git_clean.side_effect = [True, True]  # clean before and after
        args = mock.Mock(changelist='latest', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)


if __name__ == '__main__':
    unittest.main()
