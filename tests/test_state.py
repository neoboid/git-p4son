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

    def test_dismiss_is_gitignored(self):
        """The state file must be kept out of version control via a
        .gitignore inside the config dir."""
        dismiss_clobber_warning(self.ws)
        gitignore = os.path.join(self.ws, CONFIG_DIR, '.gitignore')
        self.assertTrue(os.path.exists(gitignore))
        with open(gitignore, encoding='utf-8') as f:
            entries = [line.strip() for line in f]
        self.assertIn('state.toml', entries)

    def test_gitignore_not_duplicated(self):
        dismiss_clobber_warning(self.ws)
        dismiss_clobber_warning(self.ws)
        gitignore = os.path.join(self.ws, CONFIG_DIR, '.gitignore')
        with open(gitignore, encoding='utf-8') as f:
            entries = [line.strip() for line in f if line.strip()]
        self.assertEqual(entries.count('state.toml'), 1)

    def test_dismiss_preserves_existing_gitignore_entries(self):
        os.makedirs(os.path.join(self.ws, CONFIG_DIR))
        gitignore = os.path.join(self.ws, CONFIG_DIR, '.gitignore')
        with open(gitignore, 'w', encoding='utf-8') as f:
            f.write('other.local\n')

        dismiss_clobber_warning(self.ws)

        with open(gitignore, encoding='utf-8') as f:
            entries = [line.strip() for line in f if line.strip()]
        self.assertIn('other.local', entries)
        self.assertIn('state.toml', entries)


if __name__ == '__main__':
    unittest.main()
