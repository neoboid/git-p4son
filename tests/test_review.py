"""Tests for git_p4son.review module."""

import shlex
import unittest

from git_p4son.changelist_store import load_changelist_alias, save_changelist_alias
from git_p4son.common import CommandError
from git_p4son.review import review_command, _steps
from tests.helpers import cli_args, on_jj, P4RepoTestCase, recorded, start_patch


class TestSteps(unittest.TestCase):
    def commands(self, *argv):
        return tuple(shlex.join(step) for step in _steps(cli_args(['review', '-b', 'main', *argv])))

    def test_new_then_update(self):
        self.assertEqual(self.commands('feat'),
                         ('new feat --review', 'update feat --shelve'))

    def test_force_and_no_commit_list(self):
        self.assertEqual(self.commands('feat', '--force', '--no-commit-list'),
                         ('new feat --review --force --no-commit-list',
                          'update feat --shelve --no-commit-list'))

    def test_alias_with_special_chars(self):
        first, rest = self.commands('my feature')
        self.assertIn("'my feature'", first)
        self.assertIn("'my feature'", rest)


class TestReview(P4RepoTestCase):
    """review of the two commits on branch feature, against the scripted Perforce depot."""

    def build_workspace(self):
        super().build_workspace()
        self.commit_feature()
        self.feature_head = self.git('rev-parse', 'HEAD')

    def setUp(self):
        super().setUp()
        self.sleep = start_patch(self, 'git_p4son.per_commit.time.sleep')

    def review(self, *argv):
        """Run review of my-feature with argv; returns everything it printed."""
        return self.run_command(review_command, [
            'review', 'my-feature', '-m', 'Feature', '-b', 'main', *argv])

    def review_fails(self, *argv, message):
        self.command_fails(review_command, [
            'review', 'my-feature', '-m', 'Feature', '-b', 'main', *argv], message)

    def test_new_on_the_first_commit_then_update_on_the_second(self):
        self.review()
        self.assertEqual(self.p4.pending['101'],
                         ['Feature', '', 'Changes included:', '1. Change a', '2. Replace b with c',
                          '', '#review'])
        self.assertEqual(self.p4.shelved,
                         {'101': {'a.txt': 'a2\n', 'b.txt': None, 'c.txt': 'c1\n'}})
        self.assertEqual(len(self.shelves()), 2)
        self.assertEqual(load_changelist_alias('my-feature', self.ws), '101')
        record = recorded('101', self.ws)
        self.assertEqual((record.base, len(record.included)), ('main', 2))
        self.sleep.assert_called_once_with(5)
        self.assert_at('feature', self.feature_head)

    def test_reviews_commits_where_they_are(self):
        """main moving on does not replay the commits onto it."""
        self.git('switch', '-q', 'main')
        self.commit('Unrelated')
        self.git('switch', '-q', 'feature')
        self.review()
        self.assert_at('feature', self.feature_head)

    def test_dry_run_prints_the_steps(self):
        out = self.review('--dry-run', '-m', 'Line one\nLine two')
        self.assertRegex(
            out, r'# Resolving base main\n(>  .*\n)*\[ok\] main\n')
        self.assertIn('# Changelist description\nLine one\nLine two\n'
                      '[ok] For step 1, git p4son new my-feature --review\n', out)
        self.assertRegex(out, r'Step 1 of 2: \w+ Change a\nStep 2 of 2: \w+ Replace b with c\n'
                         r'\[ok\] Would run 2 steps, git p4son new my-feature --review, then git p4son update '
                         r'my-feature --shelve\n')
        self.assertEqual(self.p4.pending, {})

    def test_no_commits(self):
        self.review_fails('-b', 'feature', message='No commits found')

    def test_existing_alias_without_force(self):
        save_changelist_alias('my-feature', '1', self.ws, force=False)
        self.review_fails(message='already exists')
        self.assertEqual(self.p4.pending, {})

    def test_dirty_workspace(self):
        self.write_file('stray.txt')
        self.review_fails(message='not clean')
        self.assertEqual(self.p4.pending, {})

    def test_failure_after_the_changelist_exists_says_how_to_resume(self):
        """The hint resumes after the last commit that worked, so earlier commits get no second shelf."""
        start_patch(self, 'git_p4son.cli.update_command',
                    side_effect=CommandError('p4 said no'))
        first_commit = self.git('rev-parse', 'HEAD~1')
        self.review_fails(message=r'Step 2 of 2 failed at \w+ Replace b with c: p4 said no\n'
                                  r'CL 101 exists. After fixing the problem, resume with: '
                                  rf'git p4son update my-feature --shelve --per-commit -b {first_commit}$')
        self.assert_at('feature', self.feature_head)

    def test_failure_before_the_changelist_exists_has_no_rerun_hint(self):
        start_patch(self, 'git_p4son.cli.new_command',
                    side_effect=CommandError('p4 said no'))
        with self.assertRaises(CommandError) as raised:
            self.run_command(review_command, [
                             'review', 'my-feature', '-m', 'Feature', '-b', 'main'])
        self.assertNotIn('resume', str(raised.exception))

    def test_failure_with_force_over_an_old_alias_has_no_rerun_hint(self):
        save_changelist_alias('my-feature', '1', self.ws, force=False)
        start_patch(self, 'git_p4son.cli.new_command',
                    side_effect=CommandError('p4 said no'))
        with self.assertRaises(CommandError) as raised:
            self.run_command(review_command, [
                             'review', 'my-feature', '-m', 'Feature', '-b', 'main', '--force'])
        self.assertNotIn('resume', str(raised.exception))


class TestReviewRelativeBase(P4RepoTestCase):
    """review with a relative base, on a feature branch above two sync commits on main."""

    def build_workspace(self):
        super().build_workspace()
        self.commit_sync(110, {'d.txt': 'd1\n'})
        self.commit_feature()

    def test_relative_base_is_resolved_where_the_command_starts(self):
        """HEAD~2 is main for every step, not the commit two before each step's commit."""
        start_patch(self, 'git_p4son.per_commit.time.sleep')
        self.run_command(review_command, [
                         'review', 'my-feature', '-m', 'Feature', '-b', 'HEAD~2'])
        self.assertEqual(self.p4.pending['111'],
                         ['Feature', '', 'Changes included:', '1. Change a', '2. Replace b with c',
                          '', '#review'])
        self.assertEqual(set(self.p4.opened), {'a.txt', 'b.txt', 'c.txt'})


_STRAY_FILE = 'jj makes a stray file part of the working change, which the loop then takes as a commit'
TestReviewOnJj = on_jj(TestReview, skip={'test_dirty_workspace': _STRAY_FILE})

if __name__ == '__main__':
    unittest.main()
