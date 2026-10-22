"""Tests for git_p4son.new module."""

import shlex
import unittest

from git_p4son.changelist_store import (
    find_record,
    list_changelist_aliases,
    load_changelist_alias,
    save_changelist_alias,
)
from git_p4son.common import CommandError
from git_p4son.new import new_command, _steps
from tests.helpers import cli_args, on_jj, P4RepoTestCase, recorded, start_patch


FEATURE_DESCRIPTION = ['Feature', '', 'Changes included:',
                       '1. Change a', '2. Replace b with c']
FEATURE_OPENED = {'a.txt': ('101', 'edit'), 'b.txt': ('101', 'delete'),
                  'c.txt': ('101', 'add')}


class NewTestCase(P4RepoTestCase):
    """new on branch feature, two commits on from main."""

    def build_workspace(self):
        super().build_workspace()
        self.commit_feature()

    def new(self, *argv, **overrides):
        """Run new for alias feat with argv; returns everything it printed."""
        return self.run_command(
            new_command, ['new', 'feat', '-m', 'Feature', '-b', 'main', *argv],
            **overrides)

    def new_fails(self, *argv, message):
        self.command_fails(
            new_command, ['new', 'feat', '-m', 'Feature', '-b', 'main', *argv],
            message)


class TestNew(NewTestCase):
    def test_creates_a_changelist_with_the_git_changes(self):
        out = self.new()
        self.assertEqual(self.p4.pending, {'101': FEATURE_DESCRIPTION})
        self.assertEqual(self.p4.opened, FEATURE_OPENED)
        self.assertEqual(self.p4.shelved, {})
        self.assertEqual(load_changelist_alias('feat', self.ws), '101')
        self.assertIn('Created CL 101 (alias=feat)', out)
        self.assertIn('Opened 3 files', out)

    def test_review_adds_the_keyword_and_shelves(self):
        self.new('--review')
        self.assertEqual(self.p4.pending['101'],
                         FEATURE_DESCRIPTION + ['', '#review'])
        self.assertEqual(self.p4.shelved,
                         {'101': {'a.txt': 'a2\n', 'b.txt': None,
                                  'c.txt': 'c1\n'}})

    def test_shelve(self):
        self.new('--shelve')
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)
        self.assertEqual(set(self.p4.shelved['101']),
                         {'a.txt', 'b.txt', 'c.txt'})

    def test_no_commit_list(self):
        self.new('--no-commit-list')
        self.assertEqual(self.p4.pending['101'], ['Feature'])

    def test_moves_files_opened_in_another_changelist(self):
        self.p4.pending['50'] = ['Other']
        self.p4.opened['a.txt'] = ('50', 'edit')
        self.new()
        self.assertEqual(self.p4.opened, FEATURE_OPENED)

    def test_dry_run_changes_nothing(self):
        out = self.new('--dry-run', '--review')
        self.assertEqual(self.p4.pending, {})
        self.assertEqual(self.p4.opened, {})
        self.assertEqual(self.p4.shelved, {})
        self.assertIn('Would create a changelist with this description '
                      '(alias=feat)', out)
        self.assertIn('2. Replace b with c', out)
        self.assertIn('Would shelve the new changelist', out)
        self.assertEqual(list_changelist_aliases(self.ws), [])


class TestNewCleanWorkspace(NewTestCase):
    def test_the_base_s_name_is_resolved_under_creating_the_changelist(self):
        out = self.new('--no-edit')
        self.assertLess(out.index('# Creating changelist'),
                        out.index('refs/heads/main'))

    def test_refuses_a_dirty_workspace_before_creating(self):
        self.edit('a.txt', 'dirty\n')
        for argv in ([], ['--dry-run']):
            with self.subTest(argv=argv):
                self.new_fails(*argv, message='not clean')
        self.assertEqual(self.p4.pending, {})

    def test_untracked_file_counts_as_dirty(self):
        self.write_file('stray.txt', 'stray\n')
        self.new_fails(message='not clean')
        self.assertEqual(self.p4.pending, {})

    def test_no_edit_skips_the_check_and_opens_nothing(self):
        self.edit('a.txt', 'dirty\n')
        self.new('--no-edit')
        self.assertEqual(self.p4.pending, {'101': FEATURE_DESCRIPTION})
        self.assertEqual(self.p4.opened, {})


class TestNewAlias(NewTestCase):
    def test_existing_alias_stops_before_creating(self):
        save_changelist_alias('feat', '1', self.ws, force=False)
        for argv in ([], ['--dry-run']):
            with self.subTest(argv=argv):
                self.new_fails(*argv, message='already exists')
        self.assertEqual(self.p4.pending, {})
        self.assertEqual(load_changelist_alias('feat', self.ws), '1')

    def test_force_replaces_an_existing_alias(self):
        save_changelist_alias('feat', '1', self.ws, force=False)
        self.new('-f')
        self.assertEqual(load_changelist_alias('feat', self.ws), '101')

    def test_records_the_base_by_name_and_the_commits_since_it(self):
        self.new('-b', self.git('rev-parse', 'main'))
        record = recorded('101', self.ws)
        # Not shelved, so there is no shelf for a later update to catch up.
        self.assertEqual((record.base, record.base_commit,
                         len(record.included)), ('main', '', 2))

    def test_a_shelved_cl_records_the_base_commit_its_shelf_is_on(self):
        self.new('--shelve')
        self.assertEqual(recorded('101', self.ws).base_commit,
                         self.git('rev-parse', 'main'))

    def test_a_base_no_branch_names_is_refused_before_creating(self):
        self.command_fails(new_command, ['new', 'feat', '-m', 'Feature', '-b', 'HEAD~1'],
                           message='-b HEAD~1 names no (branch|bookmark)')
        self.assertEqual(self.p4.pending, {})

    def test_without_an_alias_any_base_will_do(self):
        self.new('--no-alias', '-b', 'HEAD~1', alias=None)
        self.assertEqual(self.p4.pending['101'], [
                         'Feature', '', 'Changes included:', '1. Replace b with c'])
        self.assertIsNone(find_record('101', self.ws))

    def test_invalid_alias_stops_before_creating(self):
        self.command_fails(new_command, ['new', 'bad alias!', '-m', 'Feature', '-b', 'main'],
                           message='Invalid alias name')
        self.assertEqual(self.p4.pending, {})

    def test_no_alias(self):
        out = self.new('--no-alias', alias=None)
        self.assertIn('Created CL 101\n', out)
        self.assertEqual(self.p4.pending, {'101': FEATURE_DESCRIPTION})


class TestSteps(unittest.TestCase):
    def commands(self, *argv):
        return tuple(shlex.join(step) for step in _steps(cli_args(['new', '-b', 'main', *argv])))

    def test_review_shelves_each_update(self):
        self.assertEqual(self.commands('feat', '--review'),
                         ('new feat --review', 'update feat --shelve'))

    def test_shelve_shelves_each_update(self):
        self.assertEqual(self.commands('feat', '--shelve'),
                         ('new feat --shelve', 'update feat --shelve'))

    def test_review_over_shelve(self):
        self.assertEqual(self.commands('feat', '--review', '--shelve'),
                         ('new feat --review', 'update feat --shelve'))

    def test_without_shelving_neither_step_shelves(self):
        self.assertEqual(self.commands('feat'), ('new feat', 'update feat'))

    def test_force_and_no_commit_list(self):
        self.assertEqual(self.commands('feat', '--review', '--force', '--no-commit-list'),
                         ('new feat --review --force --no-commit-list',
                          'update feat --shelve --no-commit-list'))

    def test_alias_with_special_chars(self):
        first, rest = self.commands('my feature')
        self.assertIn("'my feature'", first)
        self.assertIn("'my feature'", rest)


class PerCommitTestCase(P4RepoTestCase):
    """COMMAND, a new --per-commit, of the two commits on branch feature, against the scripted Perforce depot."""

    COMMAND = ['new', 'my-feature', '--per-commit']

    def build_workspace(self):
        super().build_workspace()
        self.commit_feature()
        self.feature_head = self.git('rev-parse', 'HEAD')

    def setUp(self):
        super().setUp()
        self.sleep = start_patch(self, 'git_p4son.per_commit.time.sleep')

    def argv(self, *argv):
        return [*self.COMMAND, '-m', 'Feature', '-b', 'main', *argv]

    def new(self, *argv):
        """Run COMMAND with argv; returns everything it printed."""
        return self.run_command(new_command, self.argv(*argv))

    def new_fails(self, *argv, message):
        self.command_fails(new_command, self.argv(*argv), message)


class TestNewPerCommit(PerCommitTestCase):
    COMMAND = ['new', 'my-feature', '--review', '--per-commit']

    def test_new_on_the_first_commit_then_update_on_the_second(self):
        self.new()
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

    def test_review_runs_the_same_steps(self):
        out = self.run_command(
            new_command, ['review', 'my-feature', '-m', 'Feature', '-b', 'main'])
        self.assertIn('Ran 2 steps, git p4son new my-feature --review, then git p4son update my-feature --shelve\n',
                      out)
        self.assertEqual(len(self.shelves()), 2)

    def test_reviews_commits_where_they_are(self):
        """main moving on does not replay the commits onto it."""
        self.git('switch', '-q', 'main')
        self.commit('Unrelated')
        self.git('switch', '-q', 'feature')
        self.new()
        self.assert_at('feature', self.feature_head)

    def test_dry_run_prints_the_steps(self):
        out = self.new('--dry-run', '-m', 'Line one\nLine two')
        self.assertRegex(
            out, r'# Resolving base main\n(>  .*\n)*\[ok\] main\n')
        self.assertIn('# Changelist description\nLine one\nLine two\n'
                      '[ok] For step 1, git p4son new my-feature --review\n', out)
        self.assertRegex(out, r'Step 1 of 2: \w+ Change a\nStep 2 of 2: \w+ Replace b with c\n'
                         r'\[ok\] Would run 2 steps, git p4son new my-feature --review, then git p4son update '
                         r'my-feature --shelve\n')
        self.assertEqual(self.p4.pending, {})

    def test_no_commits(self):
        self.new_fails('-b', 'feature', message='No commits found')

    def test_existing_alias_without_force(self):
        save_changelist_alias('my-feature', '1', self.ws, force=False)
        self.new_fails(message='already exists')
        self.assertEqual(self.p4.pending, {})

    def test_dirty_workspace(self):
        self.write_file('stray.txt')
        self.new_fails(message='not clean')
        self.assertEqual(self.p4.pending, {})

    def test_failure_after_the_changelist_exists_says_how_to_resume(self):
        """The hint resumes after the last commit that worked, so earlier commits get no second shelf."""
        start_patch(self, 'git_p4son.cli.update_command',
                    side_effect=CommandError('p4 said no'))
        first_commit = self.git('rev-parse', 'HEAD~1')
        self.new_fails(message=r'Step 2 of 2 failed at \w+ Replace b with c: p4 said no\n'
                               r'CL 101 exists. After fixing the problem, resume with: '
                               rf'git p4son update my-feature --shelve --per-commit -b {first_commit}$')
        self.assert_at('feature', self.feature_head)

    def test_failure_before_the_changelist_exists_has_no_rerun_hint(self):
        start_patch(self, 'git_p4son.cli.new_command',
                    side_effect=CommandError('p4 said no'))
        with self.assertRaises(CommandError) as raised:
            self.new()
        self.assertNotIn('resume', str(raised.exception))

    def test_failure_with_force_over_an_old_alias_has_no_rerun_hint(self):
        save_changelist_alias('my-feature', '1', self.ws, force=False)
        start_patch(self, 'git_p4son.cli.new_command',
                    side_effect=CommandError('p4 said no'))
        with self.assertRaises(CommandError) as raised:
            self.new('--force')
        self.assertNotIn('resume', str(raised.exception))


class TestNewPerCommitWithoutReview(PerCommitTestCase):
    def test_shelve_shelves_once_per_commit_without_the_keyword(self):
        self.new('--shelve')
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)
        self.assertEqual(len(self.shelves()), 2)
        self.sleep.assert_called_once_with(5)

    def test_without_shelve_opens_the_files_once_per_commit_and_never_sleeps(self):
        out = self.new()
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)
        self.assertEqual(self.p4.opened, FEATURE_OPENED)
        self.assertEqual(self.p4.shelved, {})
        self.assertEqual(len(recorded('101', self.ws).included), 2)
        self.assertIn(
            'Ran 2 steps, git p4son new my-feature, then git p4son update my-feature\n', out)
        self.sleep.assert_not_called()

    def test_failure_says_how_to_resume_without_shelving(self):
        start_patch(self, 'git_p4son.cli.update_command',
                    side_effect=CommandError('p4 said no'))
        first_commit = self.git('rev-parse', 'HEAD~1')
        self.new_fails(
            message=rf'resume with: git p4son update my-feature --per-commit -b {first_commit}$')

    def test_no_edit_is_refused_before_creating(self):
        self.new_fails('--no-edit', message='--no-edit')
        self.assertEqual(self.p4.pending, {})

    def test_no_alias_is_refused_before_creating(self):
        self.new_fails('--no-alias', message='--no-alias')
        self.assertEqual(self.p4.pending, {})


class TestNewPerCommitRelativeBase(P4RepoTestCase):
    """new --review --per-commit with a relative base, on a feature branch above two sync commits on main."""

    def build_workspace(self):
        super().build_workspace()
        self.commit_sync(110, {'d.txt': 'd1\n'})
        self.commit_feature()

    def test_relative_base_is_resolved_where_the_command_starts(self):
        """HEAD~2 is main for every step, not the commit two before each step's commit."""
        start_patch(self, 'git_p4son.per_commit.time.sleep')
        self.run_command(
            new_command, [*TestNewPerCommit.COMMAND, '-m', 'Feature', '-b', 'HEAD~2'])
        self.assertEqual(self.p4.pending['111'],
                         ['Feature', '', 'Changes included:', '1. Change a', '2. Replace b with c',
                          '', '#review'])
        self.assertEqual(set(self.p4.opened), {'a.txt', 'b.txt', 'c.txt'})


_STRAY_FILE = 'jj makes a stray file part of the working change, which the loop then takes as a commit'
TestNewPerCommitOnJj = on_jj(TestNewPerCommit, skip={
                             'test_dirty_workspace': _STRAY_FILE})
TestNewOnJj = on_jj(TestNew)
TestNewAliasOnJj = on_jj(TestNewAlias)

if __name__ == '__main__':
    unittest.main()
