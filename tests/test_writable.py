"""Tests for git_p4son.writable module."""

import tempfile
import unittest

from git_p4son.config import load_config, save_config
from git_p4son.writable import is_writable_mode, set_writable_mode


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


if __name__ == '__main__':
    unittest.main()
