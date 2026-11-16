"""Tests that the test run doesn't see the developer's global git config, or the repository a hook runs it in."""

import os
import subprocess
import sys
import unittest
from unittest import mock

from tests.helpers import GitRepoTestCase

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


class TestRunFromAHook(GitRepoTestCase):
    """A git hook in a linked worktree passes GIT_DIR down, as pre-push does to the tests it runs."""

    def test_a_test_that_commits_leaves_the_hook_s_repository_alone(self):
        self.git('commit', '-q', '--allow-empty', '-m', 'Hook\'s')
        refs = self.git('for-each-ref')
        config_path = os.path.join(self.ws, '.git', 'config')
        with open(config_path) as config:
            before = config.read()
        index = os.path.join(self.ws, 'index')
        git_dir = os.path.join(self.ws, '.git')
        env = {**os.environ, 'GIT_DIR': git_dir, 'GIT_INDEX_FILE': index}
        result = subprocess.run([sys.executable, '-m', 'unittest', 'tests.test_git.TestRepoSetup.test_has_commits'],
                                cwd=_ROOT, env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git('for-each-ref'), refs)
        with open(config_path) as config:
            self.assertEqual(config.read(), before)
        self.assertFalse(os.path.exists(index))


if __name__ == '__main__':
    unittest.main()
