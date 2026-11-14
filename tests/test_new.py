"""Tests for git_p4son.new module."""

import os
import shlex
import unittest
from unittest import mock

from git_p4son.changelist_store import (
    Review,
    find_record,
    find_review,
    list_changelist_aliases,
    load_changelist_alias,
    save_changelist_alias,
)
from git_p4son.common import CommandError
from git_p4son.new import new_command, _steps
from git_p4son.update import update_command
from tests.helpers import CHECK_REVISIONS, cli_args, on_jj, OUT_OF_DATE, P4RepoTestCase, recorded, start_patch, save_config


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
        return self.command_fails(
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


class TestNewUnopened(NewTestCase):
    """new stops before shelving when p4 doesn't open a file, and update finishes the changelist once p4 takes it."""

    def test_update_finishes_after_the_cause_is_fixed(self):
        self.p4.ignored.add('c.txt')
        self.new_fails('--shelve', message=r'^p4 did not open 1 of 3 files in CL 101\. Leave the files p4 ignores '
                       r'out of the commits, or change \.p4ignore so p4 takes them\.\nCL 101 is created\. Once fixed, '
                       r'run: git p4son update feat --shelve$')
        self.assertEqual(self.p4.shelved, {})
        self.p4.ignored.clear()
        self.run_command(update_command, ['update', 'feat', '--shelve'])
        self.assertEqual(set(self.p4.shelved['101']), {
                         'a.txt', 'b.txt', 'c.txt'})
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)

    def test_without_an_alias_the_hint_names_the_changelist_and_base(self):
        self.p4.ignored.add('c.txt')
        self.p4son_fails('new', '--no-alias', '-m', 'Feature', '-b', 'main',
                         message=r'\nCL 101 is created\. Once fixed, run: git p4son update 101 -b main$')


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

    def test_a_failure_opening_the_files_leaves_the_commits_for_update(self):
        start_patch(self, 'git_p4son.new.edit_and_shelve',
                    side_effect=CommandError('p4 said no'))
        self.new_fails('--shelve', message='p4 said no')
        self.assertEqual(load_changelist_alias('feat', self.ws), '101')
        record = recorded('101', self.ws)
        self.assertEqual((record.base, record.base_commit,
                         record.included), ('main', '', []))

    def test_no_alias(self):
        out = self.new('--no-alias', alias=None)
        self.assertIn('Created CL 101\n', out)
        self.assertEqual(self.p4.pending, {'101': FEATURE_DESCRIPTION})


class TestNewReviewTool(NewTestCase):
    """new and update of a review pushed to the review tool my-tool."""

    def setUp(self):
        super().setUp()
        self.write_review_tool('my-tool')

    def update(self, *argv):
        return self.run_command(update_command, ['update', 'feat', *argv])

    def actions(self):
        return [run['args'] for run in self.review_tool_runs('my-tool')]

    def commit_change_c(self):
        self.edit('c.txt', 'c2\n')
        self.commit('Change c')

    def test_shelves_and_runs_the_tool_without_the_keyword(self):
        self.new('--review-tool', 'my-tool')
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)
        self.assertEqual(set(self.p4.shelved['101']), {
                         'a.txt', 'b.txt', 'c.txt'})
        self.assertEqual(self.actions(), [['create', '101']])
        self.assertEqual(find_review('101', self.ws),
                         Review('feat', 'my-tool', True))

    def test_review_uses_the_default_tool(self):
        save_config(self.ws, {'review-tool': {'default': 'my-tool'}})
        self.new('--review')
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)
        self.assertEqual(len(self.review_tool_runs('my-tool')), 1)

    def test_swarm_by_name_over_the_default(self):
        save_config(self.ws, {'review-tool': {'default': 'my-tool'}})
        self.new('--review-tool', 'swarm')
        self.assertEqual(self.p4.pending['101'],
                         FEATURE_DESCRIPTION + ['', '#review'])
        self.assertEqual(self.review_tool_runs('my-tool'), [])
        self.assertEqual(find_review('101', self.ws),
                         Review('feat', 'swarm', False))

    def test_a_swarm_review_is_recorded_when_swarm_is_the_default(self):
        self.new('--review')
        self.assertEqual(find_review('101', self.ws),
                         Review('feat', 'swarm', False))

    def test_shelve_without_review_runs_no_tool(self):
        save_config(self.ws, {'review-tool': {'default': 'my-tool'}})
        self.new('--shelve')
        self.update('--shelve')
        self.assertEqual(self.review_tool_runs('my-tool'), [])
        self.assertIsNone(find_review('101', self.ws))

    def test_an_unknown_tool_creates_nothing(self):
        self.new_fails('--review-tool', 'other',
                       message='Unknown review tool "other"')
        self.assertEqual(self.p4.pending, {})
        self.assertEqual(list_changelist_aliases(self.ws), [])

    def test_no_alias_is_refused_before_creating(self):
        self.new_fails('--review-tool', 'my-tool',
                       '--no-alias', message='--no-alias')
        self.assertEqual(self.p4.pending, {})

    def test_dry_run_says_which_tool_would_run(self):
        out = self.new('--review-tool', 'my-tool', '--dry-run')
        self.assertEqual(self.review_tool_runs('my-tool'), [])
        self.assertIn('Would run review tool my-tool for the new CL', out)

    def test_update_shelve_runs_the_tool_again(self):
        self.new('--review-tool', 'my-tool')
        self.commit_change_c()
        self.update('--shelve')
        self.assertEqual(self.actions(), [
                         ['create', '101'], ['update', '101']])

    def test_update_without_shelve_runs_no_tool(self):
        self.new('--review-tool', 'my-tool')
        self.commit_change_c()
        self.update()
        self.assertEqual(len(self.review_tool_runs('my-tool')), 1)

    def test_a_failed_first_run_stays_first_for_the_update_that_reruns_it(self):
        self.write_review_tool('my-tool', fail_at=1)
        self.new_fails('--review-tool', 'my-tool',
                       message='Review tool my-tool failed for CL 101')
        self.assertEqual(find_review('101', self.ws),
                         Review('feat', 'my-tool', False))
        self.assertEqual(recorded('101', self.ws).included, [])
        self.update('--shelve')
        self.assertEqual(self.actions(), [
                         ['create', '101'], ['create', '101']])
        self.assertEqual(find_review('101', self.ws),
                         Review('feat', 'my-tool', True))
        self.assertEqual(len(recorded('101', self.ws).included), 2)

    def test_the_rerun_after_a_failed_first_run_keeps_ignore_out_of_date(self):
        self.p4.submit(102, {'a.txt': 'a3\n'})
        self.write_review_tool('my-tool', fail_at=1)
        with self.assertRaises(CommandError) as raised:
            self.new('--review-tool', 'my-tool', '--ignore-out-of-date')
        hint = str(raised.exception).split(
            'Once the tool works, run: git p4son ')[1]
        self.assertIn('--ignore-out-of-date', hint)
        self.run_command(update_command, shlex.split(hint))
        self.assertEqual(len(self.actions()), 2)

    def test_a_failed_first_run_says_how_to_rerun_it(self):
        self.write_review_tool('my-tool', fail_at=1)
        with self.assertRaises(CommandError) as raised:
            self.new('--review-tool', 'my-tool')
        hint = str(raised.exception).split(
            'Once the tool works, run: git p4son ')[1]
        self.run_command(update_command, shlex.split(hint))
        self.assertEqual(self.actions(), [
                         ['create', '101'], ['create', '101']])
        self.assertEqual(len(recorded('101', self.ws).included), 2)

    def test_a_failed_update_run_leaves_its_commits_out_of_the_record(self):
        self.write_review_tool('my-tool', fail_at=2)
        self.new('--review-tool', 'my-tool')
        self.commit_change_c()
        self.command_fails(
            update_command, ['update', 'feat', '--shelve'], 'Review tool my-tool failed')
        self.assertEqual(len(recorded('101', self.ws).included), 2)
        self.update('--shelve')
        self.assertEqual(len(recorded('101', self.ws).included), 3)
        self.assertEqual(len(self.review_tool_runs('my-tool')), 3)


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

    def test_ignore_out_of_date_goes_to_both_steps(self):
        self.assertEqual(self.commands('feat', '--review', '--ignore-out-of-date'),
                         ('new feat --review --ignore-out-of-date', 'update feat --shelve --ignore-out-of-date'))

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
        return self.command_fails(new_command, self.argv(*argv), message)


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

    def test_dry_run_prints_the_steps(self):
        out = self.new('--dry-run', '-m', 'Line one\nLine two')
        self.assertRegex(
            out, r'# Resolving base name from main\n(>  .*\n)*\[ok\] main\n')
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
        """The record says which commits the CL includes, so the hinted update gives earlier ones no second shelf."""
        start_patch(self, 'git_p4son.cli.update_command',
                    side_effect=CommandError('p4 said no'))
        self.new_fails(message=r'Step 2 of 2 failed at \w+ Replace b with c: p4 said no\n'
                               r'CL 101 exists. After fixing the problem, resume with: '
                               r'git p4son update my-feature --shelve --per-commit$')
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


class TestNewPerCommitReviewTool(PerCommitTestCase):
    COMMAND = ['review', 'my-feature', '--review-tool', 'my-tool']

    def setUp(self):
        super().setUp()
        self.write_review_tool('my-tool')

    def actions(self):
        return [run['args'][0] for run in self.review_tool_runs('my-tool')]

    def fail_then_resume(self):
        """Run COMMAND, expecting it to fail, then the command its error says resumes it."""
        with self.assertRaises(CommandError) as raised:
            self.new()
        hint = str(raised.exception).split('resume with: git p4son ')[1]
        self.run_command(update_command, shlex.split(hint))

    def test_runs_the_tool_once_per_commit_without_sleeping(self):
        out = self.new()
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)
        self.assertEqual(len(self.shelves()), 2)
        self.assertEqual(self.actions(), ['create', 'update'])
        self.sleep.assert_not_called()
        self.assertIn('git p4son new my-feature --review-tool my-tool, then '
                      'git p4son update my-feature --shelve\n', out)

    def test_a_failure_in_the_first_step_resumes_with_a_first_run(self):
        self.write_review_tool('my-tool', fail_at=1)
        self.fail_then_resume()
        self.assertEqual(self.actions(), ['create', 'create', 'update'])

    def test_a_failure_in_the_first_step_gives_only_the_per_commit_hint(self):
        self.write_review_tool('my-tool', fail_at=1)
        with self.assertRaises(CommandError) as raised:
            self.new()
        self.assertNotIn('Once the tool works', str(raised.exception))
        self.assertIn(
            'resume with: git p4son update my-feature --shelve --per-commit', str(raised.exception))

    def test_a_tool_that_cannot_start_gives_the_resume_hint(self):
        self.write_review_tool(
            'my-tool', command=[os.path.join(self.ws, 'no-such-program')])
        with self.assertRaisesRegex(CommandError, 'could not start .*no-such-program.*\n.*resume with: '):
            self.new()

    def test_a_failure_in_a_later_step_resumes_at_that_commit(self):
        self.write_review_tool('my-tool', fail_at=2)
        self.fail_then_resume()
        self.assertEqual(self.actions(), ['create', 'update', 'update'])
        self.assertEqual(len(recorded('101', self.ws).included), 2)

    def test_update_per_commit_does_not_sleep(self):
        self.new()
        self.edit('c.txt', 'c2\n')
        self.commit('Change c')
        self.edit('c.txt', 'c3\n')
        self.commit('Change c again')
        self.sleep.reset_mock()
        self.run_command(update_command, [
                         'update', 'my-feature', '--shelve', '--per-commit'])
        self.assertEqual(len(self.review_tool_runs('my-tool')), 4)
        self.sleep.assert_not_called()


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
        self.new_fails(
            message=r'resume with: git p4son update my-feature --per-commit$')

    def test_the_hint_after_the_first_step_fails_past_creating_resumes(self):
        """The record includes no commit until its files are opened, so the hinted update redoes the first."""
        with mock.patch('git_p4son.new.edit_and_shelve', side_effect=CommandError('p4 said no')), \
                self.assertRaises(CommandError) as raised:
            self.run_command(new_command, self.argv('--shelve'))
        hint = str(raised.exception).split('resume with: git p4son ')[1]
        self.run_command(update_command, shlex.split(hint))
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)
        self.assertEqual(len(self.shelves()), 2)
        self.assertEqual(len(recorded('101', self.ws).included), 2)

    def test_no_edit_is_refused_before_creating(self):
        self.new_fails('--no-edit', message='--no-edit')
        self.assertEqual(self.p4.pending, {})

    def test_no_alias_is_refused_before_creating(self):
        self.new_fails('--no-alias', message='--no-alias')
        self.assertEqual(self.p4.pending, {})


class TestNewOutOfDate(NewTestCase):
    """new on a change whose files were submitted again since the workspace's last sync."""

    def test_shelving_refuses_an_edit_behind_the_head_before_creating(self):
        self.p4.submit(102, {'a.txt': 'a3\n'})
        for argv in (['--shelve'], ['--review'], ['--review', '--dry-run']):
            with self.subTest(argv=argv):
                out = self.new_fails(*argv, message=OUT_OF_DATE)
                self.assertIn('a.txt (have #1, head #2)', out)
        self.assertEqual(self.p4.pending, {})
        self.assertEqual(self.p4.opened, {})
        self.assertEqual(list_changelist_aliases(self.ws), [])

    def test_a_delete_of_a_file_deleted_at_head_is_out_of_date(self):
        self.p4.submit(102, {'b.txt': None})
        out = self.new_fails('--shelve', message=OUT_OF_DATE)
        self.assertIn('b.txt (have #1, deleted at head #2)', out)

    def test_an_add_of_a_file_someone_else_added_is_out_of_date(self):
        self.p4.submit(102, {'c.txt': 'theirs\n'})
        out = self.new_fails('--shelve', message=OUT_OF_DATE)
        self.assertIn('c.txt (not synced, head #1)', out)

    def test_files_the_change_leaves_alone_dont_count(self):
        self.p4.submit(102, {'d.txt': 'd1\n'})
        out = self.new('--shelve')
        self.assertIn(CHECK_REVISIONS, out)
        self.assertIn('3 files at the latest revision', out)
        self.assertEqual(set(self.p4.shelved['103']), {
                         'a.txt', 'b.txt', 'c.txt'})

    def test_without_shelving_there_is_no_check(self):
        self.p4.submit(102, {'a.txt': 'a3\n'})
        out = self.new()
        self.assertNotIn(CHECK_REVISIONS, out)
        self.assertEqual(self.p4.opened['a.txt'], ('103', 'edit'))

    def test_ignore_out_of_date_shelves_anyway(self):
        self.p4.submit(102, {'a.txt': 'a3\n'})
        out = self.new('--review', '--ignore-out-of-date')
        self.assertNotIn(CHECK_REVISIONS, out)
        self.assertEqual(set(self.p4.shelved['103']), {
                         'a.txt', 'b.txt', 'c.txt'})


class TestNewPerCommitOutOfDate(PerCommitTestCase):
    """new --review --per-commit, as review runs it, on a file submitted again since the last sync."""

    COMMAND = TestNewPerCommit.COMMAND

    def test_refuses_before_the_first_step(self):
        self.p4.submit(102, {'a.txt': 'a3\n'})
        out = self.new_fails(message=OUT_OF_DATE)
        self.assertNotIn('Step 1', out)
        self.assertEqual(self.p4.pending, {})

    def test_checks_once_for_all_the_commits_not_in_each_step(self):
        out = self.new()
        self.assertEqual(
            out.count(CHECK_REVISIONS), 1)
        self.assertEqual(len(self.shelves()), 2)

    def test_ignore_out_of_date_shelves_each_commit(self):
        self.p4.submit(102, {'a.txt': 'a3\n'})
        self.new('--ignore-out-of-date')
        self.assertEqual(len(self.shelves()), 2)


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


class TestNewImplicitBase(NewTestCase):
    """new and review without -b, on branch feature."""

    def test_new_takes_the_base_from_the_branch_s_local_upstream(self):
        self.git('branch', '-q', '-u', 'main')
        out = self.p4son('new', 'feat', '-m', 'Feature')
        self.assertRegex(
            out, r'# Resolving base\n(>  .*\n)*\[ok\] main \(upstream of feature\)\n')
        self.assertEqual(self.p4.pending, {'101': FEATURE_DESCRIPTION})
        self.assertEqual(recorded('101', self.ws).base, 'main')

    def test_review_takes_it_too(self):
        self.git('branch', '-q', '-u', 'main')
        out = self.p4son('review', 'feat', '-m', 'Feature', '--dry-run')
        self.assertRegex(
            out, r'# Resolving base\n(>  .*\n)*\[ok\] main \(upstream of feature\)\n')
        self.assertIn('Would run 2 steps', out)

    def test_b_wins_over_the_upstream(self):
        self.git('branch', '-q', '-u', 'main')
        self.git('branch', 'mid', 'HEAD~1')
        self.p4son('new', 'feat', '-m', 'Feature', '-b', 'mid')
        self.assertEqual(self.p4.pending['101'], [
                         'Feature', '', 'Changes included:', '1. Replace b with c'])

    def test_without_an_upstream_new_takes_the_nearest_branch_below(self):
        out = self.p4son('new', 'feat', '-m', 'Feature')
        self.assertRegex(
            out, r'# Resolving base\n(>  .*\n)*\[ok\] main \(nearest branch below\)\n')
        self.assertEqual(recorded('101', self.ws).base, 'main')

    def test_a_stacked_branch_takes_the_branch_below(self):
        self.git('switch', '-q', '-c', 'top')
        self.edit('e.txt', 'e1\n')
        self.commit('Add e')
        self.p4son('new', 'top', '-m', 'Top')
        self.assertEqual(recorded('101', self.ws).base, 'feature')
        self.assertEqual(self.p4.opened, {'e.txt': ('101', 'add')})


class TestNewOnAnOldSync(NewTestCase):
    """new and review on branch feature, forked from main before its latest sync."""

    NOT_BASED = 'not based on the latest main'

    def build_workspace(self):
        super().build_workspace()
        self.commit_sync_on_main(101, {'d.txt': 'd1\n'})

    def test_new_is_refused_before_creating(self):
        for argv in (['-b', 'main'], ['-b', 'main', '--dry-run'], ['-b', 'HEAD~2'], []):
            with self.subTest(argv=argv):
                out = self.p4son_fails(
                    'new', 'feat', '-m', 'Feature', *argv, message=self.NOT_BASED)
                self.assertNotIn('# Resolving base', out)
        self.assertEqual(self.p4.pending, {})
        self.assertEqual(self.p4.opened, {})

    def test_review_is_refused(self):
        self.p4son_fails('review', 'feat', '-m', 'Feature', '-b', 'main',
                         message=self.NOT_BASED)
        self.assertEqual(self.p4.pending, {})

    def test_no_edit_opens_nothing_so_it_runs(self):
        self.p4son('new', 'feat', '-m', 'Feature', '-b', 'main', '--no-edit')
        self.assertEqual(self.p4.pending, {'102': FEATURE_DESCRIPTION})
        self.assertEqual(self.p4.opened, {})

    def test_no_edit_on_a_stack_takes_the_branch_below(self):
        """Not rebased yet, feature..top is still only top's commit."""
        self.git('switch', '-q', '-c', 'top')
        self.edit('e.txt', 'e1\n')
        self.commit('Add e')
        out = self.p4son('new', 'top', '-m', 'Top', '--no-edit')
        self.assertRegex(
            out, r'\[ok\] feature( -> \w+)? \(nearest (branch|bookmark) below\)\n')
        self.assertEqual(self.p4.pending, {
                         '102': ['Top', '', 'Changes included:', '1. Add e']})

    def test_no_edit_per_commit_is_refused(self):
        self.p4son_fails('new', 'feat', '-m', 'Feature', '-b', 'main', '--no-edit', '--per-commit', '--review',
                         message=self.NOT_BASED)

    def test_rebased_onto_the_sync_branch_it_runs(self):
        self.git('rebase', '-q', 'main', 'feature')
        out = self.p4son('new', 'feat', '-m', 'Feature', '-b', 'main')
        self.assertRegex(
            out, r'# Checking for the latest main\n(>  .*\n)*\[ok\] .*\n\n# Resolving base\n')
        self.assertEqual(self.p4.pending, {'102': FEATURE_DESCRIPTION})

    MERGED = 'has a merge commit, [0-9a-f]{7}, above main. A changelist is made from a straight line of commits.'

    def merge_side(self):
        """Rebase feature onto main, then merge a branch with one commit on main into it."""
        self.git('rebase', '-q', 'main', 'feature')
        self.git('switch', '-q', '-c', 'side', 'main')
        self.edit('s.txt', 's1\n')
        self.commit('Add s')
        self.git('switch', '-q', 'feature')
        self.git('merge', '-q', '--no-edit', 'side')

    def assert_refused_for_a_merge(self):
        for argv in (['-b', 'main'], [], ['-b', 'main', '--dry-run'], ['-b', 'main', '--per-commit', '--shelve']):
            with self.subTest(argv=argv):
                self.p4son_fails('new', 'feat', '-m', 'Feature',
                                 *argv, message=self.MERGED)
        self.p4son_fails('review', 'feat', '-m', 'Feature',
                         '-b', 'main', message=self.MERGED)
        self.assertEqual(self.p4.pending, {})
        self.assertEqual(self.p4.opened, {})

    def test_a_merge_of_the_sync_branch_is_refused(self):
        """The commits below the merge hold the files as they were before the sync."""
        self.git('merge', '-q', '--no-edit', 'main')
        self.assert_refused_for_a_merge()

    def test_a_merge_of_another_branch_is_refused(self):
        self.merge_side()
        self.assert_refused_for_a_merge()

    def test_a_merge_below_the_base_is_refused(self):
        self.merge_side()
        self.git('switch', '-q', '-c', 'top')
        self.edit('e.txt', 'e1\n')
        self.commit('Add e')
        self.p4son_fails('new', 'top', '-m', 'Top', '-b',
                         'feature', message=self.MERGED)
        self.assertEqual(self.p4.pending, {})

    def test_per_commit_from_a_base_below_the_sync_tip_is_refused(self):
        """The first step's commit is a sync commit below the tip, whose files are older than Perforce has them."""
        self.commit_sync_on_main(102, {'e.txt': 'e1\n'})
        self.git('rebase', '-q', 'main', 'feature')
        self.git('branch', 'old', 'main~2')
        self.p4son_fails('new', 'feat', '-m', 'Feature', '-b', 'old', '--per-commit', '--shelve',
                         message='Commit [0-9a-f]{7} is not based on the latest main')
        self.assertEqual(self.p4.pending, {})

    def test_no_edit_opens_nothing_so_a_merge_runs(self):
        self.merge_side()
        self.p4son('new', 'feat', '-m', 'Feature', '-b', 'main', '--no-edit')
        self.assertEqual(self.p4.opened, {})

    def test_a_branch_stacked_on_a_rebased_one_runs(self):
        self.git('rebase', '-q', 'main', 'feature')
        self.git('switch', '-q', '-c', 'top')
        self.edit('e.txt', 'e1\n')
        self.commit('Add e')
        self.p4son('new', 'top', '-m', 'Top', '-b', 'feature')
        self.assertEqual(set(self.p4.opened), {'e.txt'})

    def test_without_the_sync_branch_there_is_nothing_to_check(self):
        save_config(self.ws, {'sync': {'branch': 'trunk'}})
        out = self.p4son('new', 'feat', '-m', 'Feature', '-b', 'main')
        self.assertRegex(out, r'\[ok\] No (branch|bookmark) trunk yet\n')
        self.assertEqual(self.p4.pending, {'102': FEATURE_DESCRIPTION})

    def test_the_sync_branch_setting_names_the_branch_to_be_based_on(self):
        self.git('branch', 'trunk', 'main')
        self.git('branch', '-q', '-f', 'main', 'feature~2')
        save_config(self.ws, {'sync': {'branch': 'trunk'}})
        self.p4son_fails('new', 'feat', '-m', 'Feature', '-b', 'main',
                         message='not based on the latest trunk.*Rebase it onto trunk')


_STRAY_FILE = 'jj makes a stray file part of the working change, which the loop then takes as a commit'
TestNewPerCommitOnJj = on_jj(TestNewPerCommit, skip={
                             'test_dirty_workspace': _STRAY_FILE})
TestNewOnJj = on_jj(TestNew)
TestNewAliasOnJj = on_jj(TestNewAlias, skip={
    'test_a_base_no_branch_names_is_refused_before_creating': 'jj records a change without a bookmark by its ID'})
TestNewOutOfDateOnJj = on_jj(TestNewOutOfDate)
TestNewOnAnOldSyncOnJj = on_jj(TestNewOnAnOldSync)

if __name__ == '__main__':
    unittest.main()
