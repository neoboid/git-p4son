"""Tests for git_p4son.writable module."""

import os
import stat
import tempfile
import unittest

from git_p4son.config import load_config, save_config
from git_p4son.writable import (
    is_writable_mode,
    make_read_only,
    make_writable,
    set_writable_mode,
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


if __name__ == '__main__':
    unittest.main()
