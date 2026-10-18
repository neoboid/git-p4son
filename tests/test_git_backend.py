"""Tests for git_p4son.git_backend and git_p4son.backend."""

import unittest
from unittest import mock

from git_p4son.backend import completion_backend, open_backend, selected_backend
from git_p4son.common import CommandError, RunError
from git_p4son.config import ConfigError
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

    def test_reads_the_change_from_head(self):
        self.assertEqual(self.backend.revision, 'HEAD')

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


class TestGitBackendBase(GitRepoTestCase):
    """GitBackend with main at "Base" and branch feat two commits on, "One" and "Two"."""

    def build_workspace(self):
        super().build_workspace()
        self.commit('Base')
        self.git('switch', '-q', '-c', 'feat')
        self.commit('One')
        self.commit('Two')

    def setUp(self):
        super().setUp()
        self.backend = GitBackend(self.ws)

    def test_a_branch_names_itself(self):
        self.assertEqual(self.backend.base_name('main'), 'main')

    def test_a_commit_is_named_by_the_one_branch_at_it(self):
        self.assertEqual(self.backend.base_name(
            self.git('rev-parse', 'main')), 'main')
        self.assertEqual(self.backend.base_name('HEAD~2'), 'main')

    def test_a_commit_with_no_branch_or_several_is_refused(self):
        with self.assertRaisesRegex(CommandError, 'HEAD~1 names no branch'):
            self.backend.base_name('HEAD~1')
        self.git('branch', 'also-main', 'main')
        with self.assertRaisesRegex(CommandError, 'HEAD~2 names no branch.*also-main, main'):
            self.backend.base_name('HEAD~2')

    def test_a_commit_s_identity_is_its_author_date(self):
        commits = [self.git('rev-parse', 'HEAD'),
                   self.git('rev-parse', 'HEAD~1')]
        dates = [self.git('log', '-1', '--format=%ad',
                          '--date=raw', commit) for commit in commits]
        self.assertEqual(self.backend.commit_identities(commits), dates)
        self.assertEqual(self.backend.commit_identities([]), [])


class TestGitBackendWithoutCommits(GitRepoTestCase):
    def test_no_subject_without_a_commit(self):
        with self.assertRaisesRegex(CommandError, 'No commits found'):
            GitBackend(self.ws).subject()


class TestChoosingTheBackend(GitRepoTestCase):
    def test_git_without_a_jj_workspace(self):
        with open_backend(self.ws, None) as backend:
            self.assertIsInstance(backend, GitBackend)
        self.assertIsInstance(completion_backend(self.ws, None), GitBackend)

    def test_setting_and_flag(self):
        self.assertEqual(selected_backend(self.ws, None), 'git')
        self.write_config('[core]\nbackend = "git"\n')
        self.assertEqual(selected_backend(self.ws, None), 'git')
        self.assertEqual(selected_backend(self.ws, 'git'), 'git')

    def test_jj_without_a_jj_workspace_is_refused(self):
        with self.assertRaisesRegex(CommandError, 'no .jj directory'):
            selected_backend(self.ws, 'jj')
        self.write_config('[core]\nbackend = "jj"\n')
        with self.assertRaisesRegex(CommandError, 'no .jj directory'):
            selected_backend(self.ws, None)

    def test_unknown_setting_is_refused(self):
        self.write_config('[core]\nbackend = "hg"\n')
        with self.assertRaisesRegex(ConfigError, 'one of auto, git, jj'):
            selected_backend(self.ws, None)


class TestGitBackendSync(GitRepoTestCase):
    def build_workspace(self):
        super().build_workspace()
        self.commit('base')

    def test_a_failed_return_after_abandoning_is_logged_not_raised(self):
        self.git('switch', '-q', '-c', 'feature')
        backend = GitBackend(self.ws)
        backend.prepare_sync(None)
        with captured_output():
            backend.start_sync()
        with mock.patch('git_p4son.git_backend.switch_branch', side_effect=RunError('git switch', 1, ['blocked'])), \
                captured_output() as out:
            backend.abandon_sync()
        self.assertIn('Could not return to feature', out.getvalue())

    def test_commit_sync_commits_everything_on_the_branch(self):
        self.write_file('a.txt', 'a\n')
        backend = GitBackend(self.ws)
        backend.prepare_sync(None)
        backend.start_sync()
        with captured_output():
            self.assertEqual(backend.commit_sync('sync subject'), 1)
        self.assertEqual(
            self.git('log', '-1', '--format=%s', 'main'), 'sync subject')
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_prepare_sync_chooses_the_sync_branch(self):
        self.git('branch', 'p4')
        self.assertEqual(GitBackend(self.ws).prepare_sync(
            None), 'refs/heads/main')
        self.assertEqual(GitBackend(
            self.ws).prepare_sync('p4'), 'refs/heads/p4')
        self.write_config('[sync]\nbranch = "p4"\n')
        self.assertEqual(GitBackend(
            self.ws).prepare_sync(None), 'refs/heads/p4')
        with self.assertRaisesRegex(CommandError, 'Sync branch "nope" does not exist'):
            GitBackend(self.ws).prepare_sync('nope')


if __name__ == '__main__':
    unittest.main()
