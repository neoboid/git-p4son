"""Tests for git_p4son.state module."""

import os
import tempfile
import unittest

from git_p4son import CONFIG_DIR
from git_p4son.state import (
    dismiss_clobber_warning,
    is_clobber_warning_dismissed,
    state_path,
)


class TestClobberWarningState(unittest.TestCase):
    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.ws = self._tempdir.name

    def tearDown(self):
        self._tempdir.cleanup()

    def test_default_is_not_dismissed(self):
        self.assertFalse(is_clobber_warning_dismissed(self.ws))

    def test_dismiss_persists(self):
        dismiss_clobber_warning(self.ws)
        self.assertTrue(is_clobber_warning_dismissed(self.ws))
        self.assertTrue(os.path.exists(state_path(self.ws)))

    def test_dismiss_ensures_config_gitignore(self):
        dismiss_clobber_warning(self.ws)
        gitignore = os.path.join(self.ws, CONFIG_DIR, '.gitignore')
        with open(gitignore, encoding='utf-8') as f:
            self.assertEqual(f.read(), '*\n')


if __name__ == '__main__':
    unittest.main()
