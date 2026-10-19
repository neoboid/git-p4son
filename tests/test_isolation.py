"""Tests that the test run doesn't see the developer's global git config."""

import os
import subprocess
import unittest
from unittest import mock

from tests.helpers import GitRepoTestCase


class TestGlobalGitConfig(GitRepoTestCase):
    """A developer's home with a global git config and ignore file, which the tests' git must not read."""

    def setUp(self):
        super().setUp()
        home = os.path.join(self.ws, 'home')
        self.write_file(os.path.join('home', '.gitconfig'),
                        '[core]\n\tautocrlf = input\n')
        self.write_file(os.path.join('home', '.config',
                        'git', 'ignore'), 'build/\n')
        patcher = mock.patch.dict(os.environ, {'HOME': home, 'USERPROFILE': home,
                                               'XDG_CONFIG_HOME': os.path.join(home, '.config')})
        patcher.start()
        self.addCleanup(patcher.stop)

    def returncode(self, *args):
        return subprocess.run(['git', *args], cwd=self.ws, capture_output=True).returncode

    def test_a_global_setting_doesn_t_apply(self):
        self.assertEqual(self.returncode(
            'config', '--global', '--get', 'core.autocrlf'), 1)

    def test_the_default_global_ignore_file_doesn_t_apply(self):
        self.write_file('build/x', 'x\n')
        self.assertEqual(self.returncode('check-ignore', '-q', 'build/x'), 1)


if __name__ == '__main__':
    unittest.main()
