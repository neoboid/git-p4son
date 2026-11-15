"""Tests for git_p4son.git_backend and git_p4son.backend."""

import os
import unittest
from unittest import mock

from git_p4son.backend import completion_backend, open_backend, selected_backend
from git_p4son.common import CommandError, RunError
from git_p4son.config import ConfigError
from git_p4son.git_backend import GitBackend
from tests.helpers import captured_output, GitRepoTestCase, save_config


class TestGitBackend(GitRepoTestCase):
    """GitBackend in a repository with one commit on main and one on branch feat/x."""

    def build_workspace(self):
        super().build_workspace()
        self.commit('Initial')
        self.git('switch', '-q', '-c', 'feat/x')
        self.commit('Add feature')

    def setUp(self):
        super().setUp()
        self.backend = GitBackend(self.ws)

    def test_reads_the_change_from_head(self):
        self.assertEqual(self.backend.revision, 'HEAD')

    def test_default_base_is_the_parent_commit(self):
        self.assertEqual(self.backend.resolve_base(None), 'HEAD~1')

    def test_a_branch_as_the_base_is_named_in_full(self):
        """So that a tag of its name, which git would resolve the bare name to, doesn't become the base."""
        self.git('tag', 'main', 'HEAD')
        self.assertEqual(self.backend.resolve_base('main'), 'refs/heads/main')
        self.assertEqual(self.backend.resolve_base(
            'feat/x'), 'refs/heads/feat/x')

    def test_a_base_that_is_no_branch_is_kept_as_given(self):
        self.git('tag', 'v1', 'main')
        for base in ('HEAD~1', 'v1', self.git('rev-parse', 'main')):
            with self.subTest(base=base):
                self.assertEqual(self.backend.resolve_base(base), base)

    def test_unknown_base_is_refused(self):
        with self.assertRaisesRegex(CommandError, '"nope" is not a commit'):
            self.backend.resolve_base('nope')

    def test_unknown_base_fails_under_its_heading(self):
        out = self.p4son_fails('list-changes', '-b', 'nope',
                               message='"nope" is not a commit')
        _, heading, section = out.partition('# Resolving base\n')
        self.assertTrue(heading)
        self.assertNotIn('[ok]', section)

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

    def test_a_change_on_the_sync_branch_s_tip_passes_the_check(self):
        with captured_output() as out:
            self.backend.check_on_sync_tip()
        self.assertRegex(out.getvalue(
        ), r'# Checking for the latest main\n(>  .*\n)+\[ok\] Based on it\n$')

    def sync_main(self):
        self.git('switch', '-q', 'main')
        self.commit('Sync')
        self.git('switch', '-q', 'feat/x')

    def test_a_change_forked_before_the_sync_branch_s_tip_is_refused(self):
        self.sync_main()
        with captured_output(), self.assertRaisesRegex(
                CommandError, 'feat/x is not based on the latest main, which is what Perforce last synced the '
                'workspace to. Rebase it onto main, or onto a branch that is rebased on it.'):
            self.backend.check_on_sync_tip()

    def test_a_detached_head_is_checked_too(self):
        self.sync_main()
        self.git('switch', '-q', '--detach')
        with captured_output(), self.assertRaisesRegex(CommandError, 'HEAD is not based on the latest main'):
            self.backend.check_on_sync_tip()

    def test_a_merge_above_the_sync_branch_is_refused(self):
        self.sync_main()
        self.git('merge', '-q', '--no-edit', 'main')
        merge = self.git('rev-parse', 'HEAD')
        self.commit('After the merge')
        with captured_output(), self.assertRaisesRegex(
                CommandError, f'feat/x has a merge commit, {merge[:7]}, above main. A changelist is made from a '
                'straight line of commits. Rebase it onto main instead of merging.'):
            self.backend.check_on_sync_tip()

    def test_a_base_on_the_sync_branch_s_tip_or_above_it_passes(self):
        self.sync_main()
        self.git('rebase', '-q', 'main')
        for base in ('main', 'feat/x', 'HEAD~1'):
            with self.subTest(base=base):
                self.backend.check_base(base, base)

    def test_a_base_below_the_sync_branch_s_tip_is_refused(self):
        self.sync_main()
        self.git('rebase', '-q', 'main')
        with self.assertRaisesRegex(
                CommandError, 'The base HEAD~2 does not contain the latest main, so the commits since it take in '
                'more than the change. Name a base on main or above it\\.$'):
            self.backend.check_base('main~1', 'HEAD~2')

    def test_a_branch_as_the_base_below_the_tip_gets_the_advice_for_a_stack(self):
        self.sync_main()
        self.git('branch', 'low', 'main~1')
        self.git('rebase', '-q', 'main')
        with self.assertRaisesRegex(
                CommandError, 'Name a base on main or above it, or if low is the branch below in a stack, point it at '
                'its rebased commit, such as with "git branch -f low <commit>"\\. Rebasing a stack from its top with '
                '--update-refs keeps the branches below with it\\.$'):
            self.backend.check_base('low', 'low')

    def test_a_base_beside_the_change_is_refused(self):
        """A branch on the sync tip that the change isn't built on: the commits since it aren't the change's."""
        self.sync_main()
        self.git('rebase', '-q', 'main')
        self.git('branch', 'beside', 'main')
        self.git('switch', '-q', 'beside')
        self.commit('Beside')
        self.git('switch', '-q', 'feat/x')
        with self.assertRaisesRegex(
                CommandError, 'The base beside is not below feat/x, such as after beside was amended or rebased, or '
                'feat/x was made from elsewhere\\. Name the base feat/x starts from, or rebase feat/x\'s own commits '
                'onto beside, such as with "git rebase --onto beside <old base> feat/x", <old base> being the commit '
                'feat/x was built on\\.$'):
            self.backend.check_base(
                self.backend.resolve_base('beside'), 'beside')

    def test_without_the_sync_branch_only_the_base_below_the_change_is_checked(self):
        self.sync_main()
        save_config(self.ws, {'sync': {'branch': 'trunk'}})
        self.backend.check_base('main~1', 'main~1')
        with self.assertRaisesRegex(CommandError, 'The base main is not below feat/x'):
            self.backend.check_base(self.backend.resolve_base('main'), 'main')

    def test_the_sync_branch_itself_passes_the_check(self):
        self.git('switch', '-q', 'main')
        with captured_output():
            self.backend.check_on_sync_tip()

    def test_without_the_sync_branch_there_is_nothing_to_check(self):
        self.sync_main()
        save_config(self.ws, {'sync': {'branch': 'trunk'}})
        with captured_output() as out:
            self.backend.check_on_sync_tip()
        self.assertIn('[ok] No branch trunk yet\n', out.getvalue())

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

    def test_a_revision_relative_to_a_branch_is_named_by_the_branch_at_its_commit(self):
        self.assertEqual(self.backend.base_name('feat~2'), 'main')
        with self.assertRaisesRegex(CommandError, 'feat~1 names no branch'):
            self.backend.base_name('feat~1')

    def test_a_revision_relative_to_a_branch_is_no_sync_branch(self):
        with self.assertRaisesRegex(CommandError, 'Sync branch "feat~1" does not exist'):
            self.backend.prepare_sync('feat~1')

    def test_a_branch_in_another_case_is_named_as_git_spells_it(self):
        """A loose ref is a file, which a case-insensitive file system finds as MAIN too; a packed one isn't."""
        if not os.path.exists(os.path.join(self.ws, '.git', 'refs', 'heads', 'MAIN')):
            self.skipTest('the file system is case-sensitive')
        self.assertEqual(self.backend.base_name('MAIN'), 'main')
        with self.assertRaisesRegex(CommandError, 'Sync branch "MAIN" does not exist'):
            self.backend.prepare_sync('MAIN')

    def test_a_commit_s_identity_is_its_author_date(self):
        commits = [self.git('rev-parse', 'HEAD'),
                   self.git('rev-parse', 'HEAD~1')]
        dates = [self.git('log', '-1', '--format=%ad',
                          '--date=raw', commit) for commit in commits]
        self.assertEqual(self.backend.commit_identities(commits), dates)
        self.assertEqual(self.backend.commit_identities([]), [])


class TestGitBackendImplicitBase(GitRepoTestCase):
    """GitBackend with main at "Base" and branch feat one commit on."""

    def build_workspace(self):
        super().build_workspace()
        self.commit('Base')
        self.git('switch', '-q', '-c', 'feat')
        self.commit_file('feat.txt')

    def setUp(self):
        super().setUp()
        self.backend = GitBackend(self.ws)

    def commit_file(self, name):
        self.write_file(name, name)
        self.commit(name)

    def stack(self):
        """Branch low off main and high off low, one commit each; HEAD ends on high."""
        self.git('switch', '-q', '-c', 'low', 'main')
        self.commit_file('low.txt')
        self.git('switch', '-q', '-c', 'high', 'low')
        self.commit_file('high.txt')

    def test_a_branch_on_the_sync_branch_takes_it(self):
        self.assertEqual(self.backend.implicit_base(), 'main')

    def test_a_deleted_branch_below_leaves_the_sync_branch(self):
        """The commits of low come along, as no branch marks where they end."""
        self.stack()
        self.git('branch', '-q', '-D', 'low')
        self.assertEqual(self.backend.implicit_base(), 'main')

    def test_a_detached_head_takes_the_nearest_branch_below(self):
        self.stack()
        self.git('switch', '-q', '--detach')
        self.assertEqual(self.backend.implicit_base(), 'low')

    def test_a_stacked_branch_takes_the_branch_below(self):
        self.stack()
        self.assertEqual(self.backend.implicit_base(), 'low')
        self.git('switch', '-q', '-c', 'top')
        self.commit_file('top.txt')
        self.assertEqual(self.backend.implicit_base(), 'high')

    def test_two_branches_on_the_nearest_commit_need_b(self):
        self.stack()
        self.git('branch', 'also', 'low')
        with self.assertRaisesRegex(CommandError, '-b is required, as also, low are the nearest branches below high. '
                                    'Name the one it starts from.'):
            self.backend.implicit_base()

    def test_the_sync_branch_moving_on_keeps_it_the_nearest(self):
        self.git('switch', '-q', 'main')
        self.commit_file('synced.txt')
        self.git('switch', '-q', 'feat')
        self.assertEqual(self.backend.implicit_base(), 'main')

    def test_without_the_sync_branch_b_is_required(self):
        save_config(self.ws, {'sync': {'branch': 'trunk'}})
        with self.assertRaisesRegex(CommandError, '-b is required, as there is no branch trunk to find the base of '
                                    'feat above. Name the base the change starts from'):
            self.backend.implicit_base()

    def test_the_sync_branch_setting_names_the_branch_the_search_stops_at(self):
        self.git('branch', 'trunk', 'main')
        save_config(self.ws, {'sync': {'branch': 'trunk'}})
        self.assertEqual(self.backend.implicit_base(), 'trunk')

    def test_a_branch_the_change_was_rebased_past_is_not_the_base(self):
        """After low is submitted and high rebased onto main, low is beside high."""
        self.stack()
        self.git('switch', '-q', 'main')
        self.commit_file('synced.txt')
        self.git('rebase', '-q', '--onto', 'main', 'low', 'high')
        self.assertEqual(self.backend.implicit_base(), 'main')


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
