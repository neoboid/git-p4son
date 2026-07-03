"""Tests for git_p4son.sync module."""

import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

from git_p4son.common import CommandError, RunError
from git_p4son.perforce import (
    P4SyncOutputProcessor,
    P4SyncPreviewFile,
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
    ChangedFile,
    LastSync,
    WritableSyncFileSet,
    _handle_clobber_warning,
    git_last_sync,
    p4_sync,
    prepare_writable_files,
    sync_command,
)
from tests.helpers import make_run_result


class TestHandleClobberWarning(unittest.TestCase):
    """The clobber warning is interactive, so it must never block
    automation and must respect a permanent dismissal."""

    def test_no_prompt_when_clobber_off(self):
        with mock.patch('git_p4son.sync.prompt_choice') as mock_prompt:
            self.assertTrue(_handle_clobber_warning(False, '/ws'))
        mock_prompt.assert_not_called()

    @mock.patch('git_p4son.sync.is_clobber_warning_dismissed',
                return_value=True)
    def test_no_prompt_when_dismissed(self, _dismissed):
        with mock.patch('git_p4son.sync.prompt_choice') as mock_prompt:
            self.assertTrue(_handle_clobber_warning(True, '/ws'))
        mock_prompt.assert_not_called()

    @mock.patch('git_p4son.sync.is_clobber_warning_dismissed',
                return_value=False)
    def test_no_prompt_when_not_a_tty(self, _dismissed):
        with mock.patch('git_p4son.sync.sys.stdin') as mock_stdin, \
                mock.patch('git_p4son.sync.prompt_choice') as mock_prompt:
            mock_stdin.isatty.return_value = False
            self.assertTrue(_handle_clobber_warning(True, '/ws'))
        mock_prompt.assert_not_called()

    @mock.patch('git_p4son.sync.dismiss_clobber_warning')
    @mock.patch('git_p4son.sync.is_clobber_warning_dismissed',
                return_value=False)
    def test_continue_persists_and_proceeds(self, _dismissed, mock_dismiss):
        """Choosing to continue implies acceptance, so it also dismisses
        the warning permanently."""
        with mock.patch('git_p4son.sync.sys.stdin') as mock_stdin, \
                mock.patch('git_p4son.sync.prompt_choice',
                           return_value='continue'):
            mock_stdin.isatty.return_value = True
            self.assertTrue(_handle_clobber_warning(True, '/ws'))
        mock_dismiss.assert_called_once_with('/ws')

    @mock.patch('git_p4son.sync.dismiss_clobber_warning')
    @mock.patch('git_p4son.sync.is_clobber_warning_dismissed',
                return_value=False)
    def test_abort_stops_sync(self, _dismissed, mock_dismiss):
        with mock.patch('git_p4son.sync.sys.stdin') as mock_stdin, \
                mock.patch('git_p4son.sync.prompt_choice',
                           return_value='abort'):
            mock_stdin.isatty.return_value = True
            self.assertFalse(_handle_clobber_warning(True, '/ws'))
        mock_dismiss.assert_not_called()

    @mock.patch('git_p4son.sync.dismiss_clobber_warning')
    @mock.patch('git_p4son.sync.is_clobber_warning_dismissed',
                return_value=False)
    def test_eof_continues_without_persisting(self, _dismissed, mock_dismiss):
        with mock.patch('git_p4son.sync.sys.stdin') as mock_stdin, \
                mock.patch('git_p4son.sync.prompt_choice', return_value=None):
            mock_stdin.isatty.return_value = True
            self.assertTrue(_handle_clobber_warning(True, '/ws'))
        mock_dismiss.assert_not_called()


def _upd(path):
    """Preview entry for a file p4 would update."""
    return P4SyncPreviewFile(mode='upd', filepath=path)


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
    def test_returns_workspace_relative_client_path(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '... depotFile //depot/foo.txt',
            '... path /ws/foo.txt',
            '... action edit',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('foo.txt', 'modify')])

    @mock.patch('git_p4son.perforce.run_with_output')
    def test_add(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '... depotFile //depot/new.txt',
            '... path /ws/new.txt',
            '... action add',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('new.txt', 'add')])

    @mock.patch('git_p4son.perforce.run_with_output')
    def test_delete(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '... depotFile //depot/old.txt',
            '... path /ws/old.txt',
            '... action delete',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('old.txt', 'delete')])

    @mock.patch('git_p4son.perforce.run_with_output')
    def test_move_add(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '... depotFile //depot/new.txt',
            '... path /ws/new.txt',
            '... action move/add',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('new.txt', 'add')])

    @mock.patch('git_p4son.perforce.run_with_output')
    def test_multiple_files(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '... depotFile //depot/a.txt',
            '... path /ws/a.txt',
            '... action edit',
            '',
            '... depotFile //depot/b.txt',
            '... path /ws/b.txt',
            '... action add',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [
            ('a.txt', 'modify'),
            ('b.txt', 'add'),
        ])

    @mock.patch('git_p4son.perforce.run_with_output')
    def test_normalizes_slashes(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '... depotFile //depot/dir/foo.txt',
            r'... path C:\ws\dir\foo.txt',
            '... action add',
        ])
        result = p4_get_opened_files('//depot', r'C:\ws')
        self.assertEqual(result, [('dir/foo.txt', 'add')])

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
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.temp_root = self._tempdir.name

    def tearDown(self):
        self._tempdir.cleanup()

    def _make_file(self, ws, name, content='content', writable=True):
        path = os.path.join(ws, name)
        with open(path, 'w') as f:
            f.write(content)
        if writable:
            os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        else:
            os.chmod(path, stat.S_IRUSR)
        return path

    def _read(self, path):
        with open(path, 'rb') as f:
            return f.read()

    @mock.patch('git_p4son.sync.get_blob_oids')
    @mock.patch('git_p4son.sync.get_file_at_commit')
    @mock.patch('git_p4son.sync.find_base_commits',
                return_value={'a.txt': 'sync456'})
    @mock.patch('git_p4son.sync.p4_fstat_file_info')
    @mock.patch('git_p4son.sync.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_user_modified_file_added_to_changed(self, _tracked, mock_fstat,
                                                 _find_base, mock_get_file,
                                                 mock_oids):
        """File modified by the user since last sync is queued for merge."""
        from git_p4son.perforce import P4FileInfo
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {
                path: P4FileInfo(head_type='text')}

            mock_oids.return_value = {('head123', 'a.txt'): 'oid_head',
                                      ('sync456', 'a.txt'): 'oid_base'}

            def fake_get_file(rel, commit, _ws):
                return b'head content' if commit == 'head123' \
                    else b'sync content'
            mock_get_file.side_effect = fake_get_file

            result = prepare_writable_files([_upd(path)], ws, 'head123',
                                            self.temp_root)

            self.assertEqual([cf.filepath for cf in result.changed], [path])
            cf = result.changed[0]
            self.assertEqual(cf.base_commit, 'sync456')
            self.assertEqual(self._read(cf.ours_path), b'head content')
            self.assertEqual(self._read(cf.base_path), b'sync content')
            self.assertEqual(result.ignored, [])
            mode = os.stat(path).st_mode
            self.assertFalse(mode & stat.S_IWUSR)

    @mock.patch('git_p4son.sync.get_blob_oids',
                return_value={('head123', 'a.txt'): 'same_oid',
                              ('sync456', 'a.txt'): 'same_oid'})
    @mock.patch('git_p4son.sync.get_file_at_commit')
    @mock.patch('git_p4son.sync.find_base_commits',
                return_value={'a.txt': 'sync456'})
    @mock.patch('git_p4son.sync.p4_fstat_file_info')
    @mock.patch('git_p4son.sync.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_user_unchanged_file_not_in_changed(self, _tracked, mock_fstat,
                                                _find_base, _get_file,
                                                _oids):
        """File whose HEAD blob matches its blob at the last sync that
        touched it is unchanged from the user's perspective."""
        from git_p4son.perforce import P4FileInfo
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {
                path: P4FileInfo(head_type='text')}

            result = prepare_writable_files([_upd(path)], ws, 'head123',
                                            self.temp_root)

            self.assertEqual(result.changed, [])
            mode = os.stat(path).st_mode
            self.assertFalse(mode & stat.S_IWUSR)

    @mock.patch('git_p4son.sync.get_file_at_commit',
                return_value=b'head content')
    @mock.patch('git_p4son.sync.find_base_commits',
                return_value={'a.txt': None})
    @mock.patch('git_p4son.sync.p4_fstat_file_info')
    @mock.patch('git_p4son.sync.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_no_baseline_treated_as_changed(self, _tracked, mock_fstat,
                                            _find_base, _get_file):
        """If no baseline commit can be found we have nothing to compare
        against and must queue for merge."""
        from git_p4son.perforce import P4FileInfo
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {
                path: P4FileInfo(head_type='text')}

            result = prepare_writable_files([_upd(path)], ws, 'head123',
                                            self.temp_root)

            self.assertEqual([cf.filepath for cf in result.changed], [path])
            cf = result.changed[0]
            self.assertIsNone(cf.base_commit)
            self.assertIsNone(cf.base_path)
            self.assertEqual(self._read(cf.ours_path), b'head content')

    @mock.patch('git_p4son.sync.get_file_at_commit',
                return_value=b'head content')
    @mock.patch('git_p4son.sync.find_base_commits',
                return_value={'image.png': None})
    @mock.patch('git_p4son.sync.p4_fstat_file_info')
    @mock.patch('git_p4son.sync.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_binary_files_tracked(self, _tracked, mock_fstat, _find_base,
                                  _get_file):
        from git_p4son.perforce import P4FileInfo
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'image.png')
            mock_fstat.return_value = {
                path: P4FileInfo(head_type='binary')}

            result = prepare_writable_files([_upd(path)], ws, 'head123',
                                            self.temp_root)

            self.assertEqual([cf.filepath for cf in result.changed], [path])
            self.assertTrue(result.changed[0].is_binary)

    @mock.patch('git_p4son.sync.get_blob_oids')
    @mock.patch('git_p4son.sync.get_file_at_commit')
    @mock.patch('git_p4son.sync.find_base_commits',
                return_value={'a.txt': 'sync456'})
    @mock.patch('git_p4son.sync.p4_fstat_file_info')
    @mock.patch('git_p4son.sync.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_crlf_workspace_stages_text_content_as_crlf(
            self, _tracked, mock_fstat, _find_base, mock_get_file,
            mock_oids):
        """With uses_crlf, staged ours/base (git LF blobs) are written as
        CRLF so the post-sync merge doesn't conflict on endings alone."""
        from git_p4son.perforce import P4FileInfo
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {path: P4FileInfo(head_type='text')}

            mock_oids.return_value = {('head123', 'a.txt'): 'oid_head',
                                      ('sync456', 'a.txt'): 'oid_base'}

            def fake_get_file(rel, commit, _ws):
                return b'a\nb\n' if commit == 'head123' else b'c\nd\n'
            mock_get_file.side_effect = fake_get_file

            result = prepare_writable_files([_upd(path)], ws, 'head123',
                                            self.temp_root, uses_crlf=True)

            cf = result.changed[0]
            self.assertEqual(self._read(cf.ours_path), b'a\r\nb\r\n')
            self.assertEqual(self._read(cf.base_path), b'c\r\nd\r\n')

    @mock.patch('git_p4son.sync.get_blob_oids')
    @mock.patch('git_p4son.sync.get_file_at_commit')
    @mock.patch('git_p4son.sync.find_base_commits',
                return_value={'a.txt': 'sync456'})
    @mock.patch('git_p4son.sync.p4_fstat_file_info')
    @mock.patch('git_p4son.sync.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_crlf_conversion_does_not_double_existing_crlf(
            self, _tracked, mock_fstat, _find_base, mock_get_file,
            mock_oids):
        """A git blob that already contains CRLF must not gain doubled \\r."""
        from git_p4son.perforce import P4FileInfo
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {path: P4FileInfo(head_type='text')}

            mock_oids.return_value = {('head123', 'a.txt'): 'oid_head',
                                      ('sync456', 'a.txt'): 'oid_base'}

            def fake_get_file(rel, commit, _ws):
                return b'a\r\nb\r\n' if commit == 'head123' else b'c\nd\n'
            mock_get_file.side_effect = fake_get_file

            result = prepare_writable_files([_upd(path)], ws, 'head123',
                                            self.temp_root, uses_crlf=True)

            cf = result.changed[0]
            self.assertEqual(self._read(cf.ours_path), b'a\r\nb\r\n')
            self.assertEqual(self._read(cf.base_path), b'c\r\nd\r\n')

    @mock.patch('git_p4son.sync.get_blob_oids')
    @mock.patch('git_p4son.sync.get_file_at_commit')
    @mock.patch('git_p4son.sync.find_base_commits',
                return_value={'image.png': 'sync456'})
    @mock.patch('git_p4son.sync.p4_fstat_file_info')
    @mock.patch('git_p4son.sync.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_crlf_workspace_leaves_binary_content_untouched(
            self, _tracked, mock_fstat, _find_base, mock_get_file,
            mock_oids):
        """Binary ours blobs are restored verbatim, so CRLF normalization
        must skip them even when the workspace uses CRLF."""
        from git_p4son.perforce import P4FileInfo
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'image.png')
            mock_fstat.return_value = {path: P4FileInfo(head_type='binary')}

            mock_oids.return_value = {
                ('head123', 'image.png'): 'oid_head',
                ('sync456', 'image.png'): 'oid_base'}

            def fake_get_file(rel, commit, _ws):
                return b'\x00a\nb\n' if commit == 'head123' \
                    else b'\x00c\nd\n'
            mock_get_file.side_effect = fake_get_file

            result = prepare_writable_files([_upd(path)], ws, 'head123',
                                            self.temp_root, uses_crlf=True)

            cf = result.changed[0]
            self.assertTrue(cf.is_binary)
            self.assertEqual(self._read(cf.ours_path), b'\x00a\nb\n')

    @mock.patch('git_p4son.sync.find_base_commits', return_value={})
    @mock.patch('git_p4son.sync.get_file_at_commit',
                return_value=b'local content')
    @mock.patch('git_p4son.sync.p4_fstat_file_info')
    @mock.patch('git_p4son.sync.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_added_both_locally_and_upstream(self, _tracked, mock_fstat,
                                             _get_file, mock_find_base):
        """A file p4 previews as 'add' over locally committed content is an
        add/add: always queued for merge against an empty base, skipping the
        git baseline comparison entirely."""
        from git_p4son.perforce import P4FileInfo
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {path: P4FileInfo(head_type='text')}

            result = prepare_writable_files(
                [P4SyncPreviewFile(mode='add', filepath=path)],
                ws, 'head123', self.temp_root)

            self.assertEqual([cf.filepath for cf in result.changed], [path])
            cf = result.changed[0]
            self.assertTrue(cf.added_both)
            self.assertIsNone(cf.base_commit)
            self.assertIsNone(cf.base_path)
            self.assertEqual(self._read(cf.ours_path), b'local content')
            # No baseline lookup happens for add/add files.
            mock_find_base.assert_called_once_with([], 'head123', ws)
            mode = os.stat(path).st_mode
            self.assertFalse(mode & stat.S_IWUSR)

    def test_readonly_files_skipped(self):
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt', writable=False)
            result = prepare_writable_files([_upd(path)], ws, 'head123',
                                            self.temp_root)

            self.assertEqual(result.changed, [])
            self.assertEqual(result.ignored, [])

    def test_untracked_files_treated_as_ignored_and_not_made_readonly(self):
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'build.log')

            with mock.patch('git_p4son.sync.get_tracked_files',
                            return_value=set()):
                result = prepare_writable_files([_upd(path)], ws, 'head123',
                                                self.temp_root)

            self.assertEqual(result.changed, [])
            self.assertEqual(result.ignored, [path])
            mode = os.stat(path).st_mode
            self.assertTrue(mode & stat.S_IWUSR)

    def _run_with_tracked_and_ignored(self, ws, clobber):
        """Prepare one unchanged tracked file plus one ignored file so the
        summary block (which reports ignored files) is reached, and return
        the concatenated warning messages."""
        tracked = self._make_file(ws, 'a.txt')
        ignored = self._make_file(ws, 'build.log')
        with mock.patch('git_p4son.sync.get_tracked_files',
                        return_value={tracked}), \
                mock.patch('git_p4son.sync.find_base_commits',
                           return_value={'a.txt': 'head123'}), \
                mock.patch('git_p4son.sync.get_blob_oids', return_value={}), \
                mock.patch('git_p4son.sync.log') as mock_log:
            result = prepare_writable_files(
                [_upd(tracked), _upd(ignored)], ws, 'head123',
                self.temp_root, clobber=clobber)
        self.assertEqual(result.ignored, [ignored])
        return ' '.join(
            str(c.args[0]) for c in mock_log.warning.call_args_list)

    def test_ignored_message_without_clobber(self):
        with tempfile.TemporaryDirectory() as ws:
            warnings = self._run_with_tracked_and_ignored(ws, clobber=False)
            self.assertIn('will not be synced', warnings)
            self.assertNotIn('overwritten', warnings)

    def test_ignored_message_with_clobber(self):
        """With clobber enabled p4 overwrites these files, so the message
        must not claim they will be preserved."""
        with tempfile.TemporaryDirectory() as ws:
            warnings = self._run_with_tracked_and_ignored(ws, clobber=True)
            self.assertIn('overwritten', warnings)
            self.assertNotIn('will not be synced', warnings)

    def test_nonexistent_files_skipped(self):
        result = prepare_writable_files(
            [_upd('/ws/noexist.txt')], '/ws', 'head123', self.temp_root)
        self.assertEqual(result.changed, [])
        self.assertEqual(result.ignored, [])

    def test_empty_input(self):
        result = prepare_writable_files([], '/ws', 'head123', self.temp_root)
        self.assertEqual(result.changed, [])
        self.assertEqual(result.ignored, [])


class TestSyncCommand(unittest.TestCase):

    _last_sync = LastSync(changelist=10000, commit='abc123')

    def setUp(self):
        # sync_command queries the client spec for line-ending handling;
        # default to "no spec" so these tests never shell out to p4.
        patcher = mock.patch('git_p4son.sync.get_client_spec',
                             return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

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
                              mock_git_clean, _git_add, _git_commit):
        mock_last_sync.return_value = self._last_sync
        mock_prep.return_value = self._empty_prep()
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

    @mock.patch('git_p4son.sync.is_file_tracked', return_value=True)
    @mock.patch('git_p4son.sync.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.p4_get_opened_files',
                return_value=[('foo.txt', 'modify')])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_dirty_p4_workspace_aborts(self, _depot, _p4clean, _git_clean,
                                       _tracked):
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
    @mock.patch('git_p4son.sync.is_file_tracked', return_value=False)
    @mock.patch('git_p4son.sync.p4_get_opened_files',
                return_value=[('ignored.bin', 'modify')])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_untracked_p4_opened_files_are_allowed(
            self, _depot, _p4clean, _tracked, mock_last_sync,
            _head, _preview, mock_prep, _p4sync,
            mock_git_clean, _add, _commit):
        mock_last_sync.return_value = LastSync(changelist=100, commit='abc')
        mock_prep.return_value = self._empty_prep()
        mock_git_clean.side_effect = [[], []]
        args = mock.Mock(changelist='200', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_sync')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_older_cl_without_force_aborts(self, _depot, _git_clean,
                                           _p4clean, mock_last_sync, _head):
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
                                          mock_git_clean, _add, _commit):
        mock_last_sync.return_value = LastSync(changelist=200, commit='abc')
        mock_prep.return_value = self._empty_prep()
        mock_git_clean.side_effect = [[], [('file.txt', 'modify')]]
        args = mock.Mock(changelist='100', force=True, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.sync._handle_clobber_warning', return_value=False)
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_sync', return_value=None)
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_aborts_when_clobber_warning_declined(
            self, _depot, _git_clean, _p4clean, _last_sync, _head,
            _warn):
        args = mock.Mock(changelist=None, force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.log')
    @mock.patch('git_p4son.sync.run_hooks')
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_sync')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_same_cl_is_noop(self, _depot, _git_clean, _p4clean,
                             mock_last_sync, _head, mock_run_hooks, mock_log):
        mock_last_sync.return_value = LastSync(changelist=100, commit='abc')
        args = mock.Mock(changelist='100', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        mock_run_hooks.assert_not_called()
        mock_log.heading.assert_any_call('Skipping post-sync hooks')

    @mock.patch('git_p4son.sync.run_hooks')
    @mock.patch('git_p4son.sync.p4_sync')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview',
                return_value=[_upd('/ws/a.txt')])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_sync')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_last_synced(self, _depot, _git_clean, _p4clean,
                         mock_last_sync, _head, _preview, mock_prep,
                         mock_p4sync, mock_run_hooks):
        mock_last_sync.return_value = LastSync(changelist=100, commit='abc')
        mock_prep.return_value = self._empty_prep()
        mock_p4sync.return_value = None
        args = mock.Mock(changelist='last-synced', force=False,
                         workspace_dir='/ws', invocation_dir='/invoked')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        mock_p4sync.assert_called_once_with(
            100, 'last synced', '//myclient', '/ws',
            expected_clobber=set())
        mock_run_hooks.assert_called_once_with('post-sync', '/ws', '/invoked')

    @mock.patch('git_p4son.sync.run_hooks')
    @mock.patch('git_p4son.sync.p4_sync')
    @mock.patch('git_p4son.sync.p4_sync_preview', return_value=[])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_sync')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.get_depot_root', return_value='//myclient')
    def test_last_synced_skips_sync_when_preview_empty(
            self, _depot, _git_clean, _p4clean, mock_last_sync,
            _head, _preview, mock_p4sync, mock_run_hooks):
        mock_last_sync.return_value = LastSync(changelist=100, commit='abc')
        args = mock.Mock(changelist='last-synced', force=False,
                         workspace_dir='/ws', invocation_dir='/invoked')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        mock_p4sync.assert_not_called()
        mock_run_hooks.assert_called_once_with('post-sync', '/ws', '/invoked')

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
                            mock_git_clean, _commit, mock_get_latest):
        mock_last_sync.return_value = LastSync(changelist=100, commit='abc')
        mock_get_latest.return_value = 200
        mock_prep.return_value = self._empty_prep()
        mock_git_clean.side_effect = [[], []]  # clean before and after
        args = mock.Mock(changelist=None, force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

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
    def test_explicit_head_keyword(self, _depot, _p4clean, mock_last_sync,
                                   _head, _preview, mock_prep, _p4sync,
                                   mock_git_clean, _commit, mock_get_latest):
        """An explicit "head" argument syncs to the latest changelist, same
        as omitting the argument."""
        mock_last_sync.return_value = LastSync(changelist=100, commit='abc')
        mock_get_latest.return_value = 200
        mock_prep.return_value = self._empty_prep()
        mock_git_clean.side_effect = [[], []]  # clean before and after
        args = mock.Mock(changelist='head', force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        mock_get_latest.assert_called_once()


class TestMergeChangedFiles(unittest.TestCase):
    """Tests for _merge_changed_files."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.temp_root = self._tempdir.name

    def tearDown(self):
        self._tempdir.cleanup()

    def _stage(self, suffix, content):
        """Write content to a temp file under self.temp_root and return its
        path. Each call uses a unique name."""
        if not hasattr(self, '_stage_counter'):
            self._stage_counter = 0
        self._stage_counter += 1
        name = f'staged_{self._stage_counter}{suffix}'
        path = os.path.join(self.temp_root, name)
        with open(path, 'wb') as f:
            f.write(content)
        return path

    @mock.patch('git_p4son.sync.merge_file')
    def test_writes_to_readonly_file_after_force_sync(self, mock_merge):
        """After p4 sync -f, files are read-only. _merge_changed_files must
        handle writing merged content to read-only files."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath = os.path.join(workspace, 'test.cpp')
            with open(filepath, 'wb') as f:
                f.write(b'perforce version')
            os.chmod(filepath, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

            mock_merge.return_value = (True, b'merged content')

            from git_p4son.sync import _merge_changed_files
            _merge_changed_files(
                [ChangedFile(
                    filepath=filepath, base_commit='sync456',
                    ours_path=self._stage('.ours', b'local version'),
                    base_path=self._stage('.base', b'sync content'))],
                workspace, self.temp_root)

            with open(filepath, 'rb') as f:
                self.assertEqual(f.read(), b'merged content')

            mock_merge.assert_called_once()

    @mock.patch('git_p4son.sync.merge_file')
    def test_binary_file_restores_user_version(self, mock_merge):
        """Binary files (identified by Perforce type) should restore the
        user's version instead of attempting a three-way merge."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath = os.path.join(workspace, 'image.png')
            with open(filepath, 'wb') as f:
                f.write(b'perforce binary content')

            from git_p4son.sync import _merge_changed_files
            _merge_changed_files(
                [ChangedFile(
                    filepath=filepath, base_commit='sync456',
                    ours_path=self._stage('.ours', b'user binary content'),
                    base_path=self._stage('.base', b'sync binary content'),
                    is_binary=True)],
                workspace, self.temp_root)

            with open(filepath, 'rb') as f:
                self.assertEqual(f.read(), b'user binary content')

            mock_merge.assert_not_called()

    def test_deleted_upstream_with_local_changes_not_restored(self):
        """When p4 deletes a file (potentially along with its parent dir),
        we don't try to restore the user's local version to disk. The local
        edits remain recoverable via git history."""
        with tempfile.TemporaryDirectory() as workspace:
            # Parent directory does not exist; emulates p4 removing it along
            # with the file.
            filepath = os.path.join(workspace, 'gone', 'test.cpp')

            from git_p4son.sync import _merge_changed_files
            _merge_changed_files(
                [ChangedFile(
                    filepath=filepath, base_commit='sync456',
                    ours_path=self._stage('.ours', b'modified locally'),
                    base_path=self._stage('.base', b'original from sync'))],
                workspace, self.temp_root)

            self.assertFalse(os.path.exists(filepath))

    def test_deleted_upstream_unchanged_local_not_restored(self):
        """When p4 deletes a file and the local version is unchanged from
        the last sync, the delete should stand (file not restored)."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath = os.path.join(workspace, 'test.cpp')
            content = b'unchanged content'

            from git_p4son.sync import _merge_changed_files
            # ours == base: file unchanged, just read-only flag cleared
            _merge_changed_files(
                [ChangedFile(
                    filepath=filepath, base_commit='sync456',
                    ours_path=self._stage('.ours', content),
                    base_path=self._stage('.base', content))],
                workspace, self.temp_root)

            # File should NOT be restored - upstream delete stands
            self.assertFalse(os.path.exists(filepath))

    def test_added_both_merges_against_empty_base(self):
        """An add/add file (no base) merges against an empty base: differing
        content yields one conflict block containing both full versions."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath = os.path.join(workspace, 'test.cpp')
            with open(filepath, 'wb') as f:
                f.write(b'p4 version\n')

            from git_p4son.sync import _merge_changed_files
            _merge_changed_files(
                [ChangedFile(filepath=filepath, base_commit=None,
                             ours_path=self._stage(
                                 '.ours', b'local version\n'),
                             base_path=None, added_both=True)],
                workspace, self.temp_root)

            with open(filepath, 'rb') as f:
                merged = f.read()
            self.assertIn(b'<<<<<<< Perforce\n', merged)
            self.assertIn(b'p4 version\n', merged)
            self.assertIn(b'local version\n', merged)
            self.assertIn(b'>>>>>>> local\n', merged)

    def test_added_both_identical_content_merges_clean(self):
        """An add/add file whose local content matches what p4 adds merges
        cleanly against the empty base."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath = os.path.join(workspace, 'test.cpp')
            with open(filepath, 'wb') as f:
                f.write(b'same content\n')

            from git_p4son.sync import _merge_changed_files
            _merge_changed_files(
                [ChangedFile(filepath=filepath, base_commit=None,
                             ours_path=self._stage(
                                 '.ours', b'same content\n'),
                             base_path=None, added_both=True)],
                workspace, self.temp_root)

            with open(filepath, 'rb') as f:
                self.assertEqual(f.read(), b'same content\n')

    @mock.patch('git_p4son.sync.merge_file')
    def test_merge_passes_staged_paths_directly(self, mock_merge):
        """The merge passes the staged ours/base temp paths straight through
        to merge_file - no extra reads or writes."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath = os.path.join(workspace, 'test.cpp')
            with open(filepath, 'wb') as f:
                f.write(b'perforce version')

            ours_path = self._stage('.ours', b'head content')
            base_path = self._stage('.base', b'intro content')

            mock_merge.return_value = (True, b'merged content')

            from git_p4son.sync import _merge_changed_files
            _merge_changed_files(
                [ChangedFile(filepath=filepath, base_commit='intro789',
                             ours_path=ours_path, base_path=base_path)],
                workspace, self.temp_root)

            mock_merge.assert_called_once_with(
                filepath, base_path, ours_path)

    @mock.patch('git_p4son.sync.merge_file')
    def test_no_base_uses_shared_empty_file(self, mock_merge):
        """When the ChangedFile has no base_path (no baseline commit), the
        merge step stages a shared empty file once and reuses it for every
        such file."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath_a = os.path.join(workspace, 'a.cpp')
            filepath_b = os.path.join(workspace, 'b.cpp')
            for p in (filepath_a, filepath_b):
                with open(p, 'wb') as f:
                    f.write(b'perforce version')

            mock_merge.return_value = (False, b'<<<<<<<\n')

            from git_p4son.sync import _merge_changed_files
            _merge_changed_files(
                [
                    ChangedFile(filepath=filepath_a, base_commit=None,
                                ours_path=self._stage('.ours_a', b'A'),
                                base_path=None),
                    ChangedFile(filepath=filepath_b, base_commit=None,
                                ours_path=self._stage('.ours_b', b'B'),
                                base_path=None),
                ],
                workspace, self.temp_root)

            # Both calls used the same empty base path.
            base_paths = [call.args[1] for call in mock_merge.call_args_list]
            self.assertEqual(len(base_paths), 2)
            self.assertEqual(base_paths[0], base_paths[1])
            # And that path points to an empty file inside temp_root.
            empty_path = base_paths[0]
            self.assertTrue(empty_path.startswith(self.temp_root))
            with open(empty_path, 'rb') as f:
                self.assertEqual(f.read(), b'')


if __name__ == '__main__':
    unittest.main()
