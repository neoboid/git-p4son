"""Tests for git_p4son.sync_merge module."""

import functools
import os
import stat
import tempfile
import unittest
from unittest import mock

from git_p4son.perforce import ClobberMode, P4SyncPreviewFile
from git_p4son.sync_merge import (
    ChangedFile,
    merge_changed_files,
    prepare_writable_files,
    report_not_synced,
    WritableSyncFileSet,
)
from tests.helpers import captured_output, preview_update


def _files_at_commits(content):
    """Fake get_files_at_commits from content(rel_path, commit, workspace_dir)."""
    return lambda items, ws: {(commit, rel): content(rel, commit, ws)
                              for commit, rel in items}


# prepare_writable_files with the noclobber, LF-only workspace most tests use.
_prepare = functools.partial(prepare_writable_files, uses_crlf=False,
                             clobber_mode=ClobberMode.NOCLOBBER)


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

    @mock.patch('git_p4son.sync_merge.get_blob_oids')
    @mock.patch('git_p4son.sync_merge.get_files_at_commits')
    @mock.patch('git_p4son.sync_merge.find_base_commits',
                return_value={'a.txt': 'sync456'})
    @mock.patch('git_p4son.sync_merge.p4_head_types')
    @mock.patch('git_p4son.sync_merge.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_user_modified_file_added_to_changed(self, _tracked, mock_fstat,
                                                 _find_base, mock_get_file,
                                                 mock_oids):
        """File modified by the user since last sync is queued for merge."""
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {
                path: 'text'}

            mock_oids.return_value = {('head123', 'a.txt'): 'oid_head',
                                      ('sync456', 'a.txt'): 'oid_base'}

            def fake_get_file(rel, commit, _ws):
                return b'head content' if commit == 'head123' \
                    else b'sync content'
            mock_get_file.side_effect = _files_at_commits(fake_get_file)

            result = _prepare([preview_update(path)], ws, 'head123',
                              self.temp_root)

            self.assertEqual([cf.filepath for cf in result.changed], [path])
            cf = result.changed[0]
            self.assertEqual(cf.base_commit, 'sync456')
            self.assertEqual(self._read(cf.ours_path), b'head content')
            self.assertEqual(self._read(cf.base_path), b'sync content')
            self.assertEqual(result.ignored, [])
            mock_get_file.assert_called_once_with(
                [('head123', 'a.txt'), ('sync456', 'a.txt')], ws)
            mode = os.stat(path).st_mode
            self.assertFalse(mode & stat.S_IWUSR)

    @mock.patch('git_p4son.sync_merge.get_blob_oids',
                return_value={('head123', 'a.txt'): 'same_oid',
                              ('sync456', 'a.txt'): 'same_oid'})
    @mock.patch('git_p4son.sync_merge.get_files_at_commits')
    @mock.patch('git_p4son.sync_merge.find_base_commits',
                return_value={'a.txt': 'sync456'})
    @mock.patch('git_p4son.sync_merge.p4_head_types')
    @mock.patch('git_p4son.sync_merge.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_user_unchanged_file_not_in_changed(self, _tracked, mock_fstat,
                                                _find_base, _get_file,
                                                _oids):
        """A file whose HEAD blob matches its baseline blob is unchanged."""
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {
                path: 'text'}

            result = _prepare([preview_update(path)], ws, 'head123',
                              self.temp_root)

            self.assertEqual(result.changed, [])
            mode = os.stat(path).st_mode
            self.assertFalse(mode & stat.S_IWUSR)

    @mock.patch('git_p4son.sync_merge.get_blob_oids',
                return_value={('head123', 'a.txt'): 'same_oid',
                              ('sync456', 'a.txt'): 'same_oid'})
    @mock.patch('git_p4son.sync_merge.get_files_at_commits')
    @mock.patch('git_p4son.sync_merge.find_base_commits',
                return_value={'a.txt': 'sync456'})
    @mock.patch('git_p4son.sync_merge.p4_head_types')
    @mock.patch('git_p4son.sync_merge.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_allwrite_keeps_unchanged_file_writable(
            self, _tracked, mock_fstat, _find_base, _get_file, _oids):
        """With allwrite an unchanged file keeps its write bit."""
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {path: 'text'}

            result = _prepare([preview_update(path)], ws, 'head123',
                              self.temp_root, clobber_mode=ClobberMode.ALLWRITE)

            self.assertEqual(result.changed, [])
            mode = os.stat(path).st_mode
            self.assertTrue(mode & stat.S_IWUSR)

    @mock.patch('git_p4son.sync_merge.get_blob_oids')
    @mock.patch('git_p4son.sync_merge.get_files_at_commits',
                side_effect=_files_at_commits(lambda *_: b'head content'))
    @mock.patch('git_p4son.sync_merge.find_base_commits',
                return_value={'a.txt': 'sync456'})
    @mock.patch('git_p4son.sync_merge.p4_head_types')
    @mock.patch('git_p4son.sync_merge.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_allwrite_makes_changed_file_read_only(
            self, _tracked, mock_fstat, _find_base, _get_file, mock_oids):
        """With allwrite a changed file still goes read-only."""
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {path: 'text'}
            mock_oids.return_value = {('head123', 'a.txt'): 'oid_head',
                                      ('sync456', 'a.txt'): 'oid_base'}

            result = _prepare([preview_update(path)], ws, 'head123',
                              self.temp_root, clobber_mode=ClobberMode.ALLWRITE)

            self.assertEqual([cf.filepath for cf in result.changed], [path])
            mode = os.stat(path).st_mode
            self.assertFalse(mode & stat.S_IWUSR)

    @mock.patch('git_p4son.sync_merge.get_blob_oids')
    @mock.patch('git_p4son.sync_merge.get_files_at_commits',
                side_effect=_files_at_commits(lambda *_: b'head content'))
    @mock.patch('git_p4son.sync_merge.find_base_commits',
                return_value={'a.txt': 'sync456'})
    @mock.patch('git_p4son.sync_merge.p4_head_types')
    @mock.patch('git_p4son.sync_merge.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_clobber_keeps_changed_file_writable(
            self, _tracked, mock_fstat, _find_base, _get_file, mock_oids):
        """With clobber p4 overwrites writable files, so even a changed one keeps its write bit."""
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {path: 'text'}
            mock_oids.return_value = {('head123', 'a.txt'): 'oid_head',
                                      ('sync456', 'a.txt'): 'oid_base'}

            result = _prepare([preview_update(path)], ws, 'head123',
                              self.temp_root, clobber_mode=ClobberMode.CLOBBER)

            self.assertEqual([cf.filepath for cf in result.changed], [path])
            mode = os.stat(path).st_mode
            self.assertTrue(mode & stat.S_IWUSR)

    @mock.patch('git_p4son.sync_merge.get_files_at_commits',
                side_effect=_files_at_commits(lambda *_: b'head content'))
    @mock.patch('git_p4son.sync_merge.find_base_commits',
                return_value={'a.txt': None})
    @mock.patch('git_p4son.sync_merge.p4_head_types')
    @mock.patch('git_p4son.sync_merge.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_no_baseline_treated_as_changed(self, _tracked, mock_fstat,
                                            _find_base, _get_file):
        """A file without a baseline commit is queued for merge."""
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {
                path: 'text'}

            result = _prepare([preview_update(path)], ws, 'head123',
                              self.temp_root)

            self.assertEqual([cf.filepath for cf in result.changed], [path])
            cf = result.changed[0]
            self.assertIsNone(cf.base_commit)
            self.assertIsNone(cf.base_path)
            self.assertEqual(self._read(cf.ours_path), b'head content')

    @mock.patch('git_p4son.sync_merge.get_files_at_commits',
                side_effect=_files_at_commits(lambda *_: b'head content'))
    @mock.patch('git_p4son.sync_merge.find_base_commits',
                return_value={'image.png': None})
    @mock.patch('git_p4son.sync_merge.p4_head_types')
    @mock.patch('git_p4son.sync_merge.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_binary_files_tracked(self, _tracked, mock_fstat, _find_base,
                                  _get_file):
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'image.png')
            mock_fstat.return_value = {
                path: 'binary'}

            result = _prepare([preview_update(path)], ws, 'head123',
                              self.temp_root)

            self.assertEqual([cf.filepath for cf in result.changed], [path])
            self.assertTrue(result.changed[0].is_binary)

    @mock.patch('git_p4son.sync_merge.get_blob_oids')
    @mock.patch('git_p4son.sync_merge.get_files_at_commits')
    @mock.patch('git_p4son.sync_merge.find_base_commits',
                return_value={'a.txt': 'sync456'})
    @mock.patch('git_p4son.sync_merge.p4_head_types')
    @mock.patch('git_p4son.sync_merge.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_crlf_workspace_stages_text_content_as_crlf(
            self, _tracked, mock_fstat, _find_base, mock_get_file,
            mock_oids):
        """In a CRLF workspace staged text content is written as CRLF."""
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {path: 'text'}

            mock_oids.return_value = {('head123', 'a.txt'): 'oid_head',
                                      ('sync456', 'a.txt'): 'oid_base'}

            def fake_get_file(rel, commit, _ws):
                return b'a\nb\n' if commit == 'head123' else b'c\nd\n'
            mock_get_file.side_effect = _files_at_commits(fake_get_file)

            result = _prepare([preview_update(path)], ws, 'head123',
                              self.temp_root, uses_crlf=True)

            cf = result.changed[0]
            self.assertEqual(self._read(cf.ours_path), b'a\r\nb\r\n')
            self.assertEqual(self._read(cf.base_path), b'c\r\nd\r\n')

    @mock.patch('git_p4son.sync_merge.get_blob_oids')
    @mock.patch('git_p4son.sync_merge.get_files_at_commits')
    @mock.patch('git_p4son.sync_merge.find_base_commits',
                return_value={'a.txt': 'sync456'})
    @mock.patch('git_p4son.sync_merge.p4_head_types')
    @mock.patch('git_p4son.sync_merge.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_crlf_conversion_does_not_double_existing_crlf(
            self, _tracked, mock_fstat, _find_base, mock_get_file,
            mock_oids):
        """A git blob that already contains CRLF must not gain doubled \\r."""
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {path: 'text'}

            mock_oids.return_value = {('head123', 'a.txt'): 'oid_head',
                                      ('sync456', 'a.txt'): 'oid_base'}

            def fake_get_file(rel, commit, _ws):
                return b'a\r\nb\r\n' if commit == 'head123' else b'c\nd\n'
            mock_get_file.side_effect = _files_at_commits(fake_get_file)

            result = _prepare([preview_update(path)], ws, 'head123',
                              self.temp_root, uses_crlf=True)

            cf = result.changed[0]
            self.assertEqual(self._read(cf.ours_path), b'a\r\nb\r\n')
            self.assertEqual(self._read(cf.base_path), b'c\r\nd\r\n')

    @mock.patch('git_p4son.sync_merge.get_blob_oids')
    @mock.patch('git_p4son.sync_merge.get_files_at_commits')
    @mock.patch('git_p4son.sync_merge.find_base_commits',
                return_value={'image.png': 'sync456'})
    @mock.patch('git_p4son.sync_merge.p4_head_types')
    @mock.patch('git_p4son.sync_merge.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_crlf_workspace_leaves_binary_content_untouched(
            self, _tracked, mock_fstat, _find_base, mock_get_file,
            mock_oids):
        """In a CRLF workspace binary content is staged verbatim."""
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'image.png')
            mock_fstat.return_value = {path: 'binary'}

            mock_oids.return_value = {
                ('head123', 'image.png'): 'oid_head',
                ('sync456', 'image.png'): 'oid_base'}

            def fake_get_file(rel, commit, _ws):
                return b'\0a\nb\n' if commit == 'head123' \
                    else b'\0c\nd\n'
            mock_get_file.side_effect = _files_at_commits(fake_get_file)

            result = _prepare([preview_update(path)], ws, 'head123',
                              self.temp_root, uses_crlf=True)

            cf = result.changed[0]
            self.assertTrue(cf.is_binary)
            self.assertEqual(self._read(cf.ours_path), b'\0a\nb\n')

    @mock.patch('git_p4son.sync_merge.find_base_commits', return_value={})
    @mock.patch('git_p4son.sync_merge.get_files_at_commits',
                side_effect=_files_at_commits(lambda *_: b'local content'))
    @mock.patch('git_p4son.sync_merge.p4_head_types')
    @mock.patch('git_p4son.sync_merge.get_tracked_files',
                side_effect=lambda paths, ws: set(paths))
    def test_added_both_locally_and_upstream(self, _tracked, mock_fstat,
                                             _get_file, mock_find_base):
        """A file p4 adds over local content is merged against an empty base."""
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'a.txt')
            mock_fstat.return_value = {path: 'text'}

            result = _prepare(
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
            result = _prepare([preview_update(path)], ws, 'head123',
                              self.temp_root)

            self.assertEqual(result.changed, [])
            self.assertEqual(result.ignored, [])

    @mock.patch('git_p4son.sync_merge.get_tracked_files')
    def test_symlink_to_writable_file_left_to_p4(self, mock_tracked):
        """A symlink is never treated as writable, so its target keeps its write bit."""
        with tempfile.TemporaryDirectory() as ws, \
                tempfile.TemporaryDirectory() as outside:
            target = os.path.join(outside, 'target.txt')
            with open(target, 'w') as f:
                f.write('outside\n')
            link = os.path.join(ws, 'link.txt')
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError):
                self.skipTest('symlinks not supported here')

            result = _prepare([preview_update(link)], ws, 'head123',
                              self.temp_root)

            self.assertEqual(result.changed, [])
            self.assertEqual(result.ignored, [])
            self.assertTrue(os.stat(target).st_mode & stat.S_IWUSR)
            mock_tracked.assert_not_called()

    def test_untracked_files_treated_as_ignored_and_not_made_readonly(self):
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'build.log')

            with mock.patch('git_p4son.sync_merge.get_tracked_files',
                            return_value=set()), \
                    mock.patch('git_p4son.sync_merge.p4_head_types',
                               return_value={}):
                result = _prepare([preview_update(path)], ws, 'head123',
                                  self.temp_root)

            self.assertEqual(result.changed, [])
            self.assertEqual(result.ignored, [path])
            self.assertEqual(result.always_writable, [])
            mode = os.stat(path).st_mode
            self.assertTrue(mode & stat.S_IWUSR)

    def test_always_writable_ignored_file_not_reported_as_unsynced(self):
        """A git-ignored +w file is not reported as unsynced."""
        with tempfile.TemporaryDirectory() as ws:
            path = self._make_file(ws, 'Tool.exe')

            with mock.patch('git_p4son.sync_merge.get_tracked_files',
                            return_value=set()), \
                    mock.patch(
                        'git_p4son.sync_merge.p4_head_types',
                        return_value={
                            path: 'binary+w'}), \
                    captured_output() as out:
                result = _prepare([preview_update(path)], ws, 'head123',
                                  self.temp_root)

            self.assertEqual(result.ignored, [])
            self.assertEqual(result.always_writable, [path])
            self.assertNotIn('will not be synced', out.getvalue())
            mode = os.stat(path).st_mode
            self.assertTrue(mode & stat.S_IWUSR)

    def test_always_writable_files_excluded_from_expected_clobber(self):
        """+w files are not expected clobber errors."""
        with tempfile.TemporaryDirectory() as ws:
            always = self._make_file(ws, 'Tool.exe')
            ignored = self._make_file(ws, 'build.log')

            with mock.patch('git_p4son.sync_merge.get_tracked_files',
                            return_value=set()), \
                    mock.patch(
                        'git_p4son.sync_merge.p4_head_types',
                        return_value={
                            always: 'binary+w',
                            ignored: 'text'}):
                result = _prepare(
                    [preview_update(always), preview_update(
                        ignored)], ws, 'head123',
                    self.temp_root)

            self.assertEqual(result.ignored, [ignored])
            self.assertEqual(result.always_writable, [always])

    def _run_with_tracked_and_ignored(self, ws, mode):
        """Prepare one unchanged tracked and one ignored file, returning what it printed."""
        tracked = self._make_file(ws, 'a.txt')
        ignored = self._make_file(ws, 'build.log')
        with mock.patch('git_p4son.sync_merge.get_tracked_files',
                        return_value={tracked}), \
                mock.patch('git_p4son.sync_merge.p4_head_types',
                           return_value={}), \
                mock.patch('git_p4son.sync_merge.find_base_commits',
                           return_value={'a.txt': 'head123'}), \
                mock.patch('git_p4son.sync_merge.get_blob_oids', return_value={}), \
                captured_output() as out:
            result = _prepare(
                [preview_update(tracked), preview_update(
                    ignored)], ws, 'head123',
                self.temp_root, clobber_mode=mode)
        self.assertEqual(result.ignored, [ignored])
        return out.getvalue()

    def test_ignored_message_without_clobber(self):
        with tempfile.TemporaryDirectory() as ws:
            warnings = self._run_with_tracked_and_ignored(
                ws, ClobberMode.NOCLOBBER)
            self.assertIn('will not be synced', warnings)
            self.assertNotIn('overwritten', warnings)

    def test_ignored_message_with_clobber(self):
        """With clobber the summary says ignored files will be overwritten."""
        with tempfile.TemporaryDirectory() as ws:
            warnings = self._run_with_tracked_and_ignored(
                ws, ClobberMode.CLOBBER)
            self.assertIn('overwritten', warnings)
            self.assertNotIn('will not be synced', warnings)

    def test_ignored_message_with_allwrite(self):
        """With allwrite the summary says ignored files sync unless modified."""
        with tempfile.TemporaryDirectory() as ws:
            out = self._run_with_tracked_and_ignored(
                ws, ClobberMode.ALLWRITE)
            self.assertNotIn('will not be synced', out)
            self.assertIn('unless modified locally', out)
            self.assertNotIn('build.log', out)

    def test_nonexistent_files_skipped(self):
        result = _prepare(
            [preview_update('/ws/noexist.txt')], '/ws', 'head123', self.temp_root)
        self.assertEqual(result.changed, [])
        self.assertEqual(result.ignored, [])

    def test_empty_input(self):
        result = _prepare([], '/ws', 'head123', self.temp_root)
        self.assertEqual(result.changed, [])
        self.assertEqual(result.ignored, [])


class TestMergeChangedFiles(unittest.TestCase):
    """Tests for merge_changed_files."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.temp_root = self._tempdir.name

    def tearDown(self):
        self._tempdir.cleanup()

    def _stage(self, suffix, content):
        """Write content to a uniquely named temp file under self.temp_root and return its path."""
        if not hasattr(self, '_stage_counter'):
            self._stage_counter = 0
        self._stage_counter += 1
        name = f'staged_{self._stage_counter}{suffix}'
        path = os.path.join(self.temp_root, name)
        with open(path, 'wb') as f:
            f.write(content)
        return path

    @mock.patch('git_p4son.sync_merge.merge_file')
    def test_writes_to_readonly_file_after_force_sync(self, mock_merge):
        """Merged content is written to files left read-only by the sync."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath = os.path.join(workspace, 'test.cpp')
            with open(filepath, 'wb') as f:
                f.write(b'perforce version')
            os.chmod(filepath, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

            mock_merge.return_value = (True, b'merged content')

            from git_p4son.sync import merge_changed_files
            merge_changed_files(
                [ChangedFile(
                    filepath=filepath, base_commit='sync456',
                    ours_path=self._stage('.ours', b'local version'),
                    base_path=self._stage('.base', b'sync content'))],
                workspace, self.temp_root)

            with open(filepath, 'rb') as f:
                self.assertEqual(f.read(), b'merged content')

            mock_merge.assert_called_once()

    @mock.patch('git_p4son.sync_merge.merge_file')
    def test_binary_file_restores_user_version(self, mock_merge):
        """A binary file gets the user's version restored instead of a three-way merge."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath = os.path.join(workspace, 'image.png')
            with open(filepath, 'wb') as f:
                f.write(b'perforce binary content')

            from git_p4son.sync import merge_changed_files
            merge_changed_files(
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
        """A file deleted upstream with local changes is not restored."""
        with tempfile.TemporaryDirectory() as workspace:
            # Emulates p4 removing the parent directory along with the file.
            filepath = os.path.join(workspace, 'gone', 'test.cpp')

            from git_p4son.sync import merge_changed_files
            merge_changed_files(
                [ChangedFile(
                    filepath=filepath, base_commit='sync456',
                    ours_path=self._stage('.ours', b'modified locally'),
                    base_path=self._stage('.base', b'original from sync'))],
                workspace, self.temp_root)

            self.assertFalse(os.path.exists(filepath))

    def test_deleted_upstream_unchanged_local_not_restored(self):
        """A file deleted upstream without local changes is not restored."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath = os.path.join(workspace, 'test.cpp')
            content = b'unchanged content'

            from git_p4son.sync import merge_changed_files
            # ours == base: file unchanged, just read-only flag cleared
            merge_changed_files(
                [ChangedFile(
                    filepath=filepath, base_commit='sync456',
                    ours_path=self._stage('.ours', content),
                    base_path=self._stage('.base', content))],
                workspace, self.temp_root)

            self.assertFalse(os.path.exists(filepath))

    def test_added_both_merges_against_empty_base(self):
        """An add/add with differing content conflicts with both full versions."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath = os.path.join(workspace, 'test.cpp')
            with open(filepath, 'wb') as f:
                f.write(b'p4 version\n')

            from git_p4son.sync import merge_changed_files
            merge_changed_files(
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
        """An add/add with identical content merges cleanly."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath = os.path.join(workspace, 'test.cpp')
            with open(filepath, 'wb') as f:
                f.write(b'same content\n')

            from git_p4son.sync import merge_changed_files
            merge_changed_files(
                [ChangedFile(filepath=filepath, base_commit=None,
                             ours_path=self._stage(
                                 '.ours', b'same content\n'),
                             base_path=None, added_both=True)],
                workspace, self.temp_root)

            with open(filepath, 'rb') as f:
                self.assertEqual(f.read(), b'same content\n')

    @mock.patch('git_p4son.sync_merge.merge_file')
    def test_merge_passes_staged_paths_directly(self, mock_merge):
        """The staged ours/base paths are passed straight to merge_file."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath = os.path.join(workspace, 'test.cpp')
            with open(filepath, 'wb') as f:
                f.write(b'perforce version')

            ours_path = self._stage('.ours', b'head content')
            base_path = self._stage('.base', b'intro content')

            mock_merge.return_value = (True, b'merged content')

            from git_p4son.sync import merge_changed_files
            merge_changed_files(
                [ChangedFile(filepath=filepath, base_commit='intro789',
                             ours_path=ours_path, base_path=base_path)],
                workspace, self.temp_root)

            mock_merge.assert_called_once_with(
                filepath, base_path, ours_path, ('Perforce', 'base', 'local'))

    @mock.patch('git_p4son.sync_merge.merge_file')
    def test_no_base_uses_shared_empty_file(self, mock_merge):
        """Files without a base share one empty base file."""
        with tempfile.TemporaryDirectory() as workspace:
            filepath_a = os.path.join(workspace, 'a.cpp')
            filepath_b = os.path.join(workspace, 'b.cpp')
            for p in (filepath_a, filepath_b):
                with open(p, 'wb') as f:
                    f.write(b'perforce version')

            mock_merge.return_value = (False, b'<<<<<<<\n')

            from git_p4son.sync import merge_changed_files
            merge_changed_files(
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


class TestMergeDeletedLocally(unittest.TestCase):
    def test_file_deleted_locally_keeps_the_perforce_version(self):
        with tempfile.TemporaryDirectory() as ws, \
                tempfile.TemporaryDirectory() as temp_root, \
                captured_output() as out:
            path = os.path.join(ws, 'a.cpp')
            with open(path, 'w') as f:
                f.write('from perforce')
            merge_changed_files(
                [ChangedFile(filepath=path, base_commit='abc', ours_path=None,
                             base_path=None)], ws, temp_root)
            with open(path) as f:
                self.assertEqual(f.read(), 'from perforce')
        self.assertIn('[warn] 1 file deleted locally but modified in Perforce\n',
                      out.getvalue())


class TestReportNotSynced(unittest.TestCase):
    def test_clobber_lists_the_ignored_files_p4_overwrote(self):
        preps = [WritableSyncFileSet(ignored=['/ws/b.log', '/ws/a.log']),
                 WritableSyncFileSet(ignored=['/ws/a.log'])]
        with captured_output() as out:
            report_not_synced(preps, ClobberMode.CLOBBER, '/ws')
        self.assertEqual(
            out.getvalue().lstrip('\n'),
            '# Git-ignored writable files overwritten by p4 (clobber enabled)\n'
            'a.log\nb.log\n'
            '[warn] 2 files overwritten\n')

    def test_nothing_to_report_prints_nothing(self):
        with captured_output() as out:
            report_not_synced([WritableSyncFileSet()],
                              ClobberMode.NOCLOBBER, '/ws')
        self.assertEqual(out.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
