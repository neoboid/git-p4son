"""Tests for git_p4son.writable module."""

import os
import stat
import subprocess
import tempfile
import unittest
from unittest import mock

from git_p4son.config import load_config, save_config
from git_p4son.writable import (
    is_writable_mode,
    make_read_only,
    make_writable,
    set_writable_mode,
    writable_command,
)


def _mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class TestWritableMode(unittest.TestCase):
    def test_off_when_not_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertFalse(is_writable_mode(tmpdir))

    def test_off_when_core_section_lacks_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            save_config(tmpdir, {'core': {'other': 'value'}})
            self.assertFalse(is_writable_mode(tmpdir))

    def test_only_a_boolean_true_enables_it(self):
        """A hand-edited string such as "yes" or "false" must not count."""
        with tempfile.TemporaryDirectory() as tmpdir:
            save_config(tmpdir, {'core': {'writable': 'false'}})
            self.assertFalse(is_writable_mode(tmpdir))

    def test_set_and_read_back(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            set_writable_mode(tmpdir, True)
            self.assertTrue(is_writable_mode(tmpdir))
            set_writable_mode(tmpdir, False)
            self.assertFalse(is_writable_mode(tmpdir))

    def test_set_preserves_other_sections(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            save_config(tmpdir, {'depot': {'root': '//ws'}})
            set_writable_mode(tmpdir, True)
            self.assertEqual(load_config(tmpdir)['depot'], {'root': '//ws'})


class TestWriteBitHelpers(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.dir = self._tempdir.name

    def tearDown(self):
        # Read-only files cannot be removed on Windows.
        for root, _dirs, names in os.walk(self.dir):
            for name in names:
                path = os.path.join(root, name)
                if not os.path.islink(path):
                    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        self._tempdir.cleanup()

    def _file(self, name, mode):
        path = os.path.join(self.dir, name)
        with open(path, 'w') as f:
            f.write('content\n')
        os.chmod(path, mode)
        return path

    def test_make_writable_changes_only_read_only_files(self):
        ro = self._file('ro.txt', stat.S_IRUSR)
        rw = self._file('rw.txt', stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(make_writable([ro, rw]), 1)
        self.assertTrue(_mode(ro) & stat.S_IWUSR)
        self.assertTrue(_mode(rw) & stat.S_IWUSR)

    def test_make_read_only_changes_only_writable_files(self):
        ro = self._file('ro.txt', stat.S_IRUSR)
        rw = self._file('rw.txt', stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(make_read_only([ro, rw]), 1)
        self.assertFalse(_mode(ro) & stat.S_IWUSR)
        self.assertFalse(_mode(rw) & stat.S_IWUSR)

    @unittest.skipIf(os.name == 'nt', 'no executable bit on Windows')
    def test_executable_bit_is_preserved(self):
        path = self._file('run.sh', stat.S_IRUSR | stat.S_IXUSR)
        make_writable([path])
        self.assertEqual(_mode(path),
                         stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        make_read_only([path])
        self.assertEqual(_mode(path), stat.S_IRUSR | stat.S_IXUSR)

    def test_missing_paths_and_directories_are_skipped(self):
        subdir = os.path.join(self.dir, 'sub')
        os.mkdir(subdir)
        missing = os.path.join(self.dir, 'missing.txt')
        self.assertEqual(make_writable([missing, subdir]), 0)
        self.assertEqual(make_read_only([missing, subdir]), 0)

    def test_symlink_target_is_never_changed(self):
        """chmod follows symlinks, so a link pointing outside the repo
        could otherwise change a file the user never asked about."""
        with tempfile.TemporaryDirectory() as outside:
            target = os.path.join(outside, 'target.txt')
            with open(target, 'w') as f:
                f.write('outside\n')
            os.chmod(target, stat.S_IRUSR)
            link = os.path.join(self.dir, 'link.txt')
            try:
                os.symlink(target, link)
            except (OSError, NotImplementedError):
                self.skipTest('symlinks not supported here')
            try:
                self.assertEqual(make_writable([link]), 0)
                self.assertFalse(_mode(target) & stat.S_IWUSR)
            finally:
                os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)


class TestWritableCommand(unittest.TestCase):
    """The writable command against a real git repo; Perforce is mocked."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.ws = self._tempdir.name
        for args in (['init'], ['config', 'user.email', 't@t.com'],
                     ['config', 'user.name', 'T']):
            subprocess.run(['git'] + args, cwd=self.ws, check=True,
                           capture_output=True)
        self.tracked = self._file('src/main.cpp')
        self.opened = self._file('src/open.cpp')
        self._file('.gitignore', 'Content/\n')
        subprocess.run(['git', 'add', '-A'], cwd=self.ws, check=True,
                       capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'init'], cwd=self.ws,
                       check=True, capture_output=True)
        self.ignored = self._file('Content/level.umap')

    def tearDown(self):
        for root, _dirs, names in os.walk(self.ws):
            for name in names:
                os.chmod(os.path.join(root, name),
                         stat.S_IRUSR | stat.S_IWUSR)
        self._tempdir.cleanup()

    def _file(self, rel, content='content\n'):
        path = os.path.join(self.ws, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(content)
        return path

    def _set_all(self, mode):
        for path in (self.tracked, self.opened, self.ignored):
            os.chmod(path, mode)

    def _writable(self, path):
        return bool(os.stat(path).st_mode & stat.S_IWUSR)

    def _run(self, action):
        return writable_command(mock.Mock(workspace_dir=self.ws,
                                          writable_action=action))

    def test_show_reports_the_mode(self):
        with mock.patch('git_p4son.writable.log') as mock_log:
            self.assertEqual(self._run(None), 0)
        mock_log.success.assert_called_with('off')
        set_writable_mode(self.ws, True)
        with mock.patch('git_p4son.writable.log') as mock_log:
            self._run(None)
        mock_log.success.assert_called_with('on')

    def test_apply_on_makes_tracked_files_writable(self):
        set_writable_mode(self.ws, True)
        self._set_all(stat.S_IRUSR)
        self.assertEqual(self._run('apply'), 0)
        self.assertTrue(self._writable(self.tracked))
        self.assertTrue(self._writable(self.opened))
        self.assertFalse(self._writable(self.ignored))

    @mock.patch('git_p4son.writable.p4_get_opened_files')
    @mock.patch('git_p4son.writable.get_client_spec')
    def test_apply_off_keeps_opened_files_writable(self, mock_spec,
                                                   mock_opened):
        """A file opened in Perforce is being worked on, so it keeps its
        write bit; ignored files are never touched."""
        mock_spec.return_value = mock.Mock(allwrite=False)
        mock_spec.return_value.name = 'ws'
        mock_opened.return_value = [('src/open.cpp', 'modify')]
        self._set_all(stat.S_IRUSR | stat.S_IWUSR)

        self.assertEqual(self._run('apply'), 0)
        self.assertFalse(self._writable(self.tracked))
        self.assertTrue(self._writable(self.opened))
        self.assertTrue(self._writable(self.ignored))
        self.assertEqual(mock_opened.call_args.args[0], '//ws')

    @mock.patch('git_p4son.writable.p4_get_opened_files')
    @mock.patch('git_p4son.writable.get_client_spec')
    def test_apply_off_leaves_allwrite_workspace_alone(self, mock_spec,
                                                       mock_opened):
        mock_spec.return_value = mock.Mock(allwrite=True)
        self._set_all(stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(self._run('apply'), 0)
        self.assertTrue(self._writable(self.tracked))
        mock_opened.assert_not_called()

    def test_enable_turns_mode_on_and_applies(self):
        self._set_all(stat.S_IRUSR)
        self.assertEqual(self._run('enable'), 0)
        self.assertTrue(is_writable_mode(self.ws))
        self.assertTrue(self._writable(self.tracked))
        self.assertFalse(self._writable(self.ignored))

    @mock.patch('git_p4son.writable.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.writable.get_client_spec')
    def test_disable_turns_mode_off_and_applies(self, mock_spec, _opened):
        mock_spec.return_value = mock.Mock(allwrite=False)
        mock_spec.return_value.name = 'ws'
        set_writable_mode(self.ws, True)
        self._set_all(stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(self._run('disable'), 0)
        self.assertFalse(is_writable_mode(self.ws))
        self.assertFalse(self._writable(self.tracked))

    @mock.patch('git_p4son.writable.get_client_spec', return_value=None)
    def test_apply_off_needs_a_perforce_workspace(self, _spec):
        self._set_all(stat.S_IRUSR | stat.S_IWUSR)
        self.assertEqual(self._run('apply'), 1)
        self.assertTrue(self._writable(self.tracked))


if __name__ == '__main__':
    unittest.main()
