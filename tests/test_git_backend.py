"""Tests for git_p4son.git_backend and git_p4son.backend."""

import unittest

from git_p4son.backend import open_backend
from git_p4son.common import CommandError
from git_p4son.git_backend import GitBackend
from tests.helpers import captured_output, GitRepoTestCase


class TestGitBackend(GitRepoTestCase):
    """GitBackend in a repository with one commit on branch feat/x."""

    def build_workspace(self):
        super().build_workspace()
        self.git('switch', '-q', '-c', 'feat/x')
        self.commit('Add feature')

    def setUp(self):
        super().setUp()
        self.backend = GitBackend(self.ws)

    def test_reads_the_change_from_head_and_the_index(self):
        self.assertIsNone(self.backend.revision)

    def test_clean_check_lists_uncommitted_files(self):
        with captured_output():
            self.backend.check_clean()
        self.write_file('stray.txt')
        with captured_output() as out, self.assertRaisesRegex(CommandError, 'not clean'):
            self.backend.check_clean()
        self.assertIn('stray.txt', out.getvalue())


class TestChoosingTheBackend(GitRepoTestCase):
    def test_git_without_a_jj_workspace(self):
        with open_backend(self.ws, None) as backend:
            self.assertIsInstance(backend, GitBackend)


if __name__ == '__main__':
    unittest.main()
