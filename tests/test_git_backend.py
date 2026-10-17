"""Tests for git_p4son.git_backend and git_p4son.backend."""

import unittest

from git_p4son.backend import completion_backend, open_backend
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

    def test_default_base_is_the_parent_commit(self):
        self.assertEqual(self.backend.resolve_base(None), 'HEAD~1')

    def test_named_base_is_kept_as_given(self):
        self.assertEqual(self.backend.resolve_base('main'), 'main')

    def test_suggested_alias_is_the_branch_with_slashes_replaced(self):
        self.assertEqual(self.backend.suggested_alias(), 'feat-x')

    def test_detached_head_has_no_suggested_alias(self):
        self.git('switch', '-q', '--detach')
        with self.assertRaisesRegex(CommandError, 'detached HEAD'):
            self.backend.suggested_alias()

    def test_reserved_branch_name_has_no_suggested_alias(self):
        self.git('switch', '-q', '-c', 'last-synced')
        with self.assertRaisesRegex(CommandError, 'reserved keyword "last-synced"'):
            self.backend.suggested_alias()

    def test_subject_is_the_head_commit_subject(self):
        self.assertEqual(self.backend.subject(), 'Add feature')

    def test_clean_check_lists_uncommitted_files(self):
        with captured_output():
            self.backend.check_clean()
        self.write_file('stray.txt')
        with captured_output() as out, self.assertRaisesRegex(CommandError, 'not clean'):
            self.backend.check_clean()
        self.assertIn('stray.txt', out.getvalue())

    def test_base_candidates_ask_the_shell_for_branches(self):
        self.assertEqual(self.backend.base_candidates(), [('__branch__', '')])

    def test_words(self):
        self.assertEqual(
            (self.backend.words.tool, self.backend.words.branch), ('git', 'branch'))


class TestGitBackendWithoutCommits(GitRepoTestCase):
    def test_no_subject_without_a_commit(self):
        with self.assertRaisesRegex(CommandError, 'No commits found'):
            GitBackend(self.ws).subject()


class TestChoosingTheBackend(GitRepoTestCase):
    def test_git_without_a_jj_workspace(self):
        with open_backend(self.ws, None) as backend:
            self.assertIsInstance(backend, GitBackend)
        self.assertIsInstance(completion_backend(
            self.ws, ignore_jj=False), GitBackend)


if __name__ == '__main__':
    unittest.main()
