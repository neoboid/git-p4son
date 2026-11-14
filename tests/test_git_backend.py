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

    def test_named_base_is_kept_as_given(self):
        self.assertEqual(self.backend.resolve_base('main'), 'main')

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

    def test_given_commits_each_is_checked_and_the_first_stale_one_named(self):
        self.sync_main()
        stale = self.git('rev-parse', 'HEAD')
        with captured_output() as out:
            self.backend.check_on_sync_tip([self.git('rev-parse', 'main')])
        self.assertIn(
            '# Checking each commit for the latest main\n', out.getvalue())
        with captured_output(), self.assertRaisesRegex(
                CommandError, f'Commit {stale[:7]} is not based on the latest main'):
            self.backend.check_on_sync_tip(
                [self.git('rev-parse', 'main'), stale])

    def test_a_detached_head_is_checked_too(self):
        self.sync_main()
        self.git('switch', '-q', '--detach')
        with captured_output(), self.assertRaisesRegex(CommandError, 'HEAD is not based on the latest main'):
            self.backend.check_on_sync_tip()

    def test_a_merge_of_the_sync_branch_s_tip_passes_the_check(self):
        self.sync_main()
        self.git('merge', '-q', '--no-edit', 'main')
        with captured_output():
            self.backend.check_on_sync_tip()

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
    """GitBackend with main at "Base" and branch feat one commit on, tracking main."""

    def build_workspace(self):
        super().build_workspace()
        self.commit('Base')
        self.git('switch', '-q', '-c', 'feat', '--track', 'main')
        self.commit_file('feat.txt')

    def setUp(self):
        super().setUp()
        self.backend = GitBackend(self.ws)

    def commit_file(self, name):
        self.write_file(name, name)
        self.commit(name)

    def stack(self, sync_branch='main', track=True):
        """Branch low off sync_branch and high off low, one commit each, as upstreams if track; HEAD ends on high."""
        tracking = '--track' if track else '--no-track'
        self.git('switch', '-q', '-c', 'low', tracking, sync_branch)
        self.commit_file('low.txt')
        self.git('switch', '-q', '-c', 'high', tracking, 'low')
        self.commit_file('high.txt')

    def test_the_local_upstream_is_the_base(self):
        self.assertEqual(self.backend.implicit_base(),
                         ('main', 'upstream of feat'))

    def test_a_branch_on_the_sync_branch_without_an_upstream_takes_it(self):
        self.git('branch', '--unset-upstream')
        self.assertEqual(self.backend.implicit_base(),
                         ('main', 'nearest branch below'))

    def test_a_remote_upstream_does_not_count(self):
        self.git('remote', 'add', 'origin', self.ws)
        self.git('fetch', '-q', 'origin')
        self.git('branch', '-q', '-u', 'origin/main')
        self.assertEqual(self.backend.implicit_base(),
                         ('main', 'nearest branch below'))

    def test_a_deleted_upstream_does_not_count(self):
        """The commits of low come along, as no branch marks where they end."""
        self.stack()
        self.git('branch', '-q', '-D', 'low')
        self.assertEqual(self.backend.implicit_base(),
                         ('main', 'nearest branch below'))

    def test_a_detached_head_takes_the_nearest_branch_below(self):
        self.stack(track=False)
        self.git('switch', '-q', '--detach')
        self.assertEqual(self.backend.implicit_base(),
                         ('low', 'nearest branch below'))

    def test_a_stacked_branch_without_an_upstream_takes_the_branch_below(self):
        self.stack(track=False)
        self.assertEqual(self.backend.implicit_base(),
                         ('low', 'nearest branch below'))
        self.git('switch', '-q', '-c', 'top')
        self.commit_file('top.txt')
        self.assertEqual(self.backend.implicit_base(),
                         ('high', 'nearest branch below'))

    def test_the_upstream_wins_over_a_nearer_branch(self):
        self.stack()
        self.commit_file('high2.txt')
        self.git('branch', 'nearer', 'HEAD~1')
        self.assertEqual(self.backend.implicit_base(),
                         ('low', 'upstream of high'))

    def test_two_branches_on_the_nearest_commit_need_b(self):
        self.stack(track=False)
        self.git('branch', 'also', 'low')
        with self.assertRaisesRegex(CommandError, '-b is required, as also, low are the nearest branches below high. '
                                    'Name the one it starts from.'):
            self.backend.implicit_base()

    def test_a_nearest_branch_below_a_merge_of_the_sync_branch_needs_b(self):
        """low..HEAD would take in the sync commit the merge brought in."""
        self.stack(track=False)
        self.git('switch', '-q', 'main')
        self.commit_file('synced.txt')
        self.git('switch', '-q', 'high')
        self.git('merge', '-q', '--no-edit', 'main')
        with self.assertRaisesRegex(CommandError, '-b is required, as low, the nearest branch below high, would take '
                                    'in commits of main, such as after a merge of it. Name the base'):
            self.backend.implicit_base()

    def test_a_stack_the_sync_branch_moved_on_from_keeps_the_branch_below(self):
        """As with --no-edit before a rebase: low..HEAD is still only high's commit."""
        self.stack(track=False)
        self.git('switch', '-q', 'main')
        self.commit_file('synced.txt')
        self.git('switch', '-q', 'high')
        self.assertEqual(self.backend.implicit_base(),
                         ('low', 'nearest branch below'))

    def test_the_sync_branch_moving_on_keeps_it_the_nearest(self):
        self.git('branch', '--unset-upstream')
        self.git('switch', '-q', 'main')
        self.commit_file('synced.txt')
        self.git('switch', '-q', 'feat')
        self.assertEqual(self.backend.implicit_base(),
                         ('main', 'nearest branch below'))

    def test_without_an_upstream_and_the_sync_branch_b_is_required(self):
        self.git('branch', '--unset-upstream')
        save_config(self.ws, {'sync': {'branch': 'trunk'}})
        with self.assertRaisesRegex(CommandError, '-b is required, as feat has no local upstream and there is no '
                                    'branch trunk to find the base above. Name the base the change starts from'):
            self.backend.implicit_base()

    def test_the_sync_branch_setting_names_the_branch_the_search_stops_at(self):
        self.git('branch', '--unset-upstream')
        self.git('branch', 'trunk', 'main')
        save_config(self.ws, {'sync': {'branch': 'trunk'}})
        self.assertEqual(self.backend.implicit_base(),
                         ('trunk', 'nearest branch below'))

    def test_the_sync_branch_moving_on_keeps_it_the_base(self):
        self.git('switch', '-q', 'main')
        self.commit_file('synced.txt')
        self.git('switch', '-q', 'feat')
        self.assertEqual(self.backend.implicit_base(),
                         ('main', 'upstream of feat'))

    def assert_not_below_is_refused(self):
        with self.assertRaisesRegex(CommandError, '-b is required, as low, the upstream of high, is not below it'):
            self.backend.implicit_base()

    def test_a_parent_branch_below_is_the_base(self):
        self.stack()
        self.assertEqual(self.backend.implicit_base(),
                         ('low', 'upstream of high'))

    def test_an_amended_parent_branch_is_refused(self):
        """low..HEAD would take in the old copy of low's commit."""
        self.stack()
        self.git('switch', '-q', 'low')
        self.git('commit', '-q', '--amend', '-m', 'low.txt v2')
        self.git('switch', '-q', 'high')
        self.assert_not_below_is_refused()

    def test_a_parent_branch_rebased_onto_the_sync_branch_is_refused(self):
        self.stack()
        self.git('switch', '-q', 'main')
        self.commit_file('synced.txt')
        self.git('rebase', '-q', 'main', 'low')
        self.git('switch', '-q', 'high')
        self.assert_not_below_is_refused()

    def test_a_parent_branch_that_gained_commits_is_refused(self):
        """Not below HEAD, which is all a default can check; -b low takes it."""
        self.stack()
        self.git('switch', '-q', 'low')
        self.commit_file('low2.txt')
        self.git('switch', '-q', 'high')
        self.assert_not_below_is_refused()

    def test_an_upstream_the_branch_was_rebased_past_is_refused(self):
        """After low is submitted and high rebased onto main, low..HEAD would take in low's old commit."""
        self.stack()
        self.git('switch', '-q', 'main')
        self.commit_file('synced.txt')
        self.git('rebase', '-q', '--onto', 'main', 'low', 'high')
        self.assert_not_below_is_refused()

    def assert_left_on_an_old_sync_commit_is_refused(self, sync_branch):
        """old is below high, but in the sync branch's history, so old..HEAD would take in the sync commits."""
        self.git('branch', 'old', sync_branch)
        self.git('switch', '-q', sync_branch)
        self.commit_file('synced.txt')
        self.git('switch', '-q', '-c', 'high')
        self.git('branch', '-q', '-u', 'old')
        self.commit_file('high.txt')
        with self.assertRaisesRegex(
                CommandError, f'old, the upstream of high, is in the history of {sync_branch}.*'
                f'git branch -u {sync_branch}'):
            self.backend.implicit_base()

    def test_an_upstream_below_a_merge_of_the_sync_branch_is_refused(self):
        """low..HEAD would take in the sync commit the merge brought in."""
        self.stack()
        self.git('switch', '-q', 'main')
        self.commit_file('synced.txt')
        self.git('switch', '-q', 'high')
        self.git('merge', '-q', '--no-edit', 'main')
        with self.assertRaisesRegex(CommandError, 'low, the upstream of high, would take in commits of main'):
            self.backend.implicit_base()

    def test_an_upstream_left_on_an_old_sync_commit_is_refused(self):
        self.assert_left_on_an_old_sync_commit_is_refused('main')

    def test_the_sync_branch_setting_names_the_branch_the_upstream_is_checked_against(self):
        self.git('branch', 'trunk', 'main')
        save_config(self.ws, {'sync': {'branch': 'trunk'}})
        self.assert_left_on_an_old_sync_commit_is_refused('trunk')


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
