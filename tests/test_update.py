"""Tests for git_p4son.update module."""

import shlex
import unittest
from unittest import mock

from git_p4son.changelist_store import find_record, Record, save_changelist_alias
from git_p4son.common import CommandError
from git_p4son.git import author_dates
from git_p4son.new import new_command
from git_p4son.update import _step, update_command
from tests.helpers import (
    on_jj,
    captured_output,
    cli_args,
    GitRepoTestCase,
    JjTestCase,
    P4RepoTestCase,
    recorded,
    start_patch,
)


def _args(**overrides):
    defaults = dict(workspace_dir='/ws', changelist='100', dry_run=False,
                    base_branch='HEAD~1', no_edit=False,
                    shelve=False, message=None, no_commit_list=False,
                    file=None, per_commit=False)
    defaults.update(overrides)
    return cli_args(['update'], **defaults)


FEATURE_DESCRIPTION = ['Feature', '', 'Changes included:',
                       '1. Change a', '2. Replace b with c']
FEATURE_OPENED = {'a.txt': ('101', 'edit'), 'b.txt': ('101', 'delete'),
                  'c.txt': ('101', 'add')}


class UpdateTestCase(P4RepoTestCase):
    """update of CL 101, which new created for the two commits on branch feature."""

    def build_workspace(self):
        super().build_workspace()
        self.commit_feature()
        self.run_command(
            new_command, ['new', 'feat', '-m', 'Feature', '-b', 'main'])

    def update(self, *argv):
        """Run update of CL 101 with argv; returns everything it printed."""
        return self.run_command(update_command, ['update', '101', '-b', 'main', *argv])

    def update_fails(self, *argv, message):
        self.command_fails(update_command, ['update', '101', '-b', 'main', *argv],
                           message)

    def commit_change_c(self):
        self.edit('c.txt', 'c2\n')
        self.commit('Change c')


class TestUpdate(UpdateTestCase):
    def test_merges_new_commits_into_the_description(self):
        self.commit_change_c()
        out = self.update()
        self.assertEqual(self.p4.pending['101'],
                         FEATURE_DESCRIPTION + ['3. Change c'])
        self.assertIn('Updated CL 101', out)
        self.assertEqual(self.p4.shelved, {})

    def test_message_replaces_the_message(self):
        self.update('-m', 'New title')
        self.assertEqual(self.p4.pending['101'],
                         ['New title'] + FEATURE_DESCRIPTION[1:])

    def test_no_commit_list_leaves_the_description_alone(self):
        self.p4.pending['101'] = ['Edited by hand']
        self.commit_change_c()
        self.update('--no-commit-list')
        self.assertEqual(self.p4.pending['101'], ['Edited by hand'])
        self.assertNotIn(['change', '-o', '101'], self.p4.calls)

    def test_message_with_no_commit_list_keeps_the_list(self):
        self.commit_change_c()
        self.update('-m', 'New title', '--no-commit-list')
        self.assertEqual(self.p4.pending['101'],
                         ['New title'] + FEATURE_DESCRIPTION[1:])

    def test_alias_names_the_changelist(self):
        self.commit_change_c()
        out = self.run_command(
            update_command, ['update', 'feat', '-b', 'main'])
        self.assertIn('feat -> CL 101', out)
        self.assertEqual(self.p4.pending['101'][-1], '3. Change c')

    def test_reverts_files_no_longer_changed_before_shelving(self):
        self.edit('a.txt', 'a1\n')
        self.write_file('b.txt', 'b1\n')
        self.commit('Restore a and b')
        out = self.update('--shelve')
        self.assertEqual(self.p4.opened, {'c.txt': ('101', 'add')})
        self.assertEqual(self.p4.shelved, {'101': {'c.txt': 'c1\n'}})
        self.assertEqual(self.read('b.txt'), 'b1\n')
        self.assertIn('2 files reverted', out)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_dry_run_changes_nothing(self):
        self.edit('a.txt', 'a1\n')
        self.commit('Restore a')
        out = self.update('--dry-run', '--shelve')
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)
        self.assertEqual(self.p4.opened['a.txt'], ('101', 'edit'))
        self.assertEqual(self.p4.shelved, {})
        self.assertIn('Would update CL 101 with this description', out)
        self.assertIn('3. Restore a', out)
        self.assertIn('1 file would be reverted', out)
        self.assertIn('Would shelve CL 101', out)


class TestUpdateCleanWorkspace(UpdateTestCase):
    def test_refuses_a_dirty_workspace_before_touching_the_changelist(self):
        self.commit_change_c()
        self.edit('a.txt', 'dirty\n')
        for argv in ([], ['--dry-run']):
            with self.subTest(argv=argv):
                self.update_fails(*argv, message='not clean')
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)
        self.assertEqual(self.p4.calls, [])

    def test_a_dirty_workspace_is_refused_before_the_plan_is_checked(self):
        """Checked once, before the command runs, so ahead of "already includes"."""
        self.edit('a.txt', 'dirty\n')
        self.command_fails(update_command, [
                           'update', '101', '--per-commit'], message='not clean')

    def test_no_edit_skips_the_check_and_opens_nothing(self):
        self.edit('d.txt', 'd1\n')
        self.commit('Add d')
        self.edit('a.txt', 'dirty\n')
        self.update('--no-edit')
        # The alias records the CL's commits, so Add d waits for an update that opens its files.
        self.assertEqual(self.p4.pending['101'][-1], '2. Replace b with c')
        self.assertNotIn('d.txt', self.p4.opened)

    def test_no_edit_shelves_without_reverting(self):
        self.edit('a.txt', 'a1\n')
        self.commit('Restore a')
        self.update('--no-edit', '--shelve')
        self.assertEqual(self.p4.opened, FEATURE_OPENED)
        self.assertEqual(set(self.p4.shelved['101']),
                         {'a.txt', 'b.txt', 'c.txt'})


class TestUpdateChecksOnce(UpdateTestCase):
    """The workspace checks of an update with a record, counted by their heading."""

    @property
    def CHECK(self):
        return '# Checking jj workspace files' if isinstance(self, JjTestCase) else '# Checking git workspace'

    def setUp(self):
        super().setUp()
        start_patch(self, 'git_p4son.per_commit.time.sleep')
        self.commit_change_c()

    def test_a_shelving_update_checks_once(self):
        self.assertEqual(self.update('--shelve').count(self.CHECK), 1)

    def per_commit_checks(self, *argv):
        """How many times update --per-commit with argv checks, over Change c and Add d."""
        self.edit('d.txt', 'd1\n')
        self.commit('Add d')
        return self.run_command(update_command, ['update', '101', '--per-commit', *argv]).count(self.CHECK)

    def test_a_shelving_per_commit_update_checks_once_not_in_its_steps(self):
        self.assertEqual(self.per_commit_checks('--shelve'), 1)

    def test_a_per_commit_update_checks_once_not_in_its_steps(self):
        self.assertEqual(self.per_commit_checks(), 1)


class TestSteps(unittest.TestCase):
    def commands(self, **overrides):
        return shlex.join(_step('100', _args(**overrides)))

    def test_plain_update(self):
        self.assertEqual(self.commands(), 'update 100')

    def test_flags(self):
        self.assertEqual(self.commands(no_commit_list=True, no_edit=True, shelve=True),
                         'update 100 --no-commit-list --no-edit --shelve')


class TestUpdatePerCommit(UpdateTestCase):
    """update --per-commit of CL 101 over the two commits on branch feature, its alias without a record."""

    def setUp(self):
        super().setUp()
        save_changelist_alias('feat', '101', self.ws, force=True)
        self.sleep = start_patch(self, 'git_p4son.per_commit.time.sleep')
        self.feature_head = self.git('rev-parse', 'HEAD')
        self.p4.calls.clear()

    def update(self, *argv):
        return super().update('--per-commit', *argv)

    def test_updates_once_per_commit_and_returns(self):
        self.p4.pending['101'] = ['Feature']
        self.update('--shelve')
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)
        self.assertEqual(self.p4.opened, FEATURE_OPENED)
        self.assertEqual(len(self.shelves()), 2)
        self.sleep.assert_called_once_with(5)
        self.assert_at('feature', self.feature_head)

    def test_sleeps_only_when_shelving(self):
        self.update()
        self.assertEqual(self.shelves(), [])
        self.sleep.assert_not_called()

    def test_message_goes_to_the_first_step_only(self):
        messages = []
        real = update_command

        def record_message(args):
            messages.append(args.message)
            return real(args)
        start_patch(self, 'git_p4son.cli.update_command',
                    side_effect=record_message)
        self.update('-m', 'One\nTwo')
        self.assertEqual(messages, ['One\nTwo', None])
        self.assertEqual(self.p4.pending['101'][:2], ['One', 'Two'])

    def test_alias_resolved_once(self):
        self.run_command(update_command, [
                         'update', 'feat', '--per-commit', '-b', 'main'])
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)

    def test_no_commits(self):
        self.command_fails(update_command, ['update', '101', '--per-commit', '-b', 'feature'],
                           message='No commits found')

    def test_failure_says_how_to_resume(self):
        """The hint resumes after the last commit that worked, with the same flags."""
        calls = []
        real = update_command

        def fail_second(args):
            calls.append(args)
            if len(calls) == 2:
                raise CommandError('p4 said no')
            return real(args)
        start_patch(self, 'git_p4son.cli.update_command',
                    side_effect=fail_second)
        first_commit = self.git('rev-parse', 'HEAD~1')
        self.command_fails(update_command, ['update', '101', '--per-commit', '--shelve', '-b', 'main'],
                           message=r'Step 2 of 2 failed at \w+ Replace b with c: p4 said no\n'
                                   r'After fixing the problem, resume with: '
                                   rf'git p4son update 101 --shelve --per-commit -b {first_commit}$')

    def fail_first_step(self, argv, **overrides):
        """Run update with argv and the first step failing; returns the error message."""
        start_patch(self, 'git_p4son.cli.update_command',
                    side_effect=CommandError('p4 said no'))
        with captured_output(), self.assertRaises(CommandError) as raised:
            update_command(cli_args(argv, workspace_dir=self.ws, **overrides))
        return str(raised.exception)

    def test_failed_first_step_resumes_with_the_message(self):
        """The description may not have been saved, so the hint passes the message on."""
        main = self.git('rev-parse', 'main')
        error = self.fail_first_step(
            ['update', '101', '--per-commit', '-b', 'main', '-m', '-WIP title'])
        self.assertTrue(error.endswith(
            f"resume with: git p4son update 101 --per-commit '--message=-WIP title' -b {main}"), error)

    def test_failed_first_step_resumes_with_the_message_file(self):
        main = self.git('rev-parse', 'main')
        error = self.fail_first_step(['update', '101', '--per-commit', '-b', 'main', '-F', '/tmp/msg.txt'],
                                     message='From the file')
        self.assertTrue(error.endswith(
            f'resume with: git p4son update 101 --per-commit -F /tmp/msg.txt -b {main}'), error)

    def test_dry_run_prints_the_steps(self):
        out = self.update('--dry-run')
        self.assertIn('Change a: git p4son update 101', out)
        self.assertIn('Replace b with c: git p4son update 101', out)
        self.assertEqual(self.p4.calls, [])


class TestUpdatePerCommitRelativeBase(P4RepoTestCase):
    """update --per-commit with a relative base, on a feature branch above two sync commits on main."""

    def build_workspace(self):
        super().build_workspace()
        self.commit_sync(110, {'d.txt': 'd1\n'})
        self.commit_feature()
        self.run_command(
            new_command, ['new', 'feat', '-m', 'Feature', '-b', 'main', '--no-edit'])

    def setUp(self):
        super().setUp()
        save_changelist_alias('feat', '111', self.ws, force=True)

    def test_relative_base_is_resolved_where_the_command_starts(self):
        """HEAD~2 is main for every step, not the commit two before each step's commit."""
        self.run_command(update_command, [
                         'update', 'feat', '--per-commit', '-b', 'HEAD~2'])
        self.assertEqual(self.p4.pending['111'], FEATURE_DESCRIPTION)
        self.assertEqual(set(self.p4.opened), {'a.txt', 'b.txt', 'c.txt'})


class IncludedTestCase(UpdateTestCase):
    """update of CL 101, whose alias new made records main as the base and the two commits since it."""

    def setUp(self):
        super().setUp()
        start_patch(self, 'git_p4son.per_commit.time.sleep')

    def update(self, *argv):
        return self.run_command(update_command, ['update', 'feat', *argv])

    def update_fails(self, *argv, message):
        self.command_fails(update_command, ['update', 'feat', *argv], message)

    def reword_first_entry(self):
        """Reword the first entry in the CL's commit list, as a user does in P4V."""
        self.p4.pending['101'] = ['Feature', '', 'Changes included:',
                                  '1. Change a, reworded', '2. Replace b with c']

    def included(self):
        """How many commits the record says CL 101 includes, leaving out the slots of dropped ones."""
        return len([identity for identity in recorded('101', self.ws).included if identity])


class TestUpdateIncluded(IncludedTestCase):
    """update on a record, run on git and on jj."""

    def test_new_records_the_base_and_the_commits_since_it(self):
        with self.command_args(['update', 'feat']) as args:
            identities = args.backend.commit_identities(
                [self.git('rev-parse', 'HEAD~1'), self.git('rev-parse', 'HEAD')])
        self.assertEqual(find_record('101', self.ws),
                         ('feat', Record('main', '', identities)))

    def test_update_appends_only_the_new_commits_leaving_the_entries_as_they_are(self):
        self.reword_first_entry()
        self.commit_change_c()
        self.update()
        self.assertEqual(self.p4.pending['101'][3:], [
                         '1. Change a, reworded', '2. Replace b with c', '3. Change c'])
        self.assertIn('c.txt', self.p4.opened)
        self.assertEqual(self.included(), 3)

    def test_planning_has_a_heading_closed_by_what_is_new(self):
        self.commit_change_c()
        # Up to the section's first ok line, as a jj template echoed holds a newline.
        section = self.update().split('# Planning the update of CL 101\n', 1)[
            1].split('\n\n', 1)[0]
        self.assertTrue(section.endswith(
            '[ok] 2 commits included, 1 new'), section)

    def test_an_explicit_base_reopens_the_files_but_still_only_appends(self):
        self.reword_first_entry()
        self.commit_change_c()
        self.update('-b', 'main')
        self.assertEqual(self.p4.pending['101'][3:], [
                         '1. Change a, reworded', '2. Replace b with c', '3. Change c'])
        self.assertEqual(set(self.p4.opened), {'a.txt', 'b.txt', 'c.txt'})

    def test_per_commit_shelves_only_the_new_commits(self):
        self.commit_change_c()
        self.edit('d.txt', 'd1\n')
        self.commit('Add d')
        self.update('--per-commit', '--shelve')
        self.assertEqual(len(self.shelves()), 2)
        self.assertEqual(self.p4.pending['101'][3:], [
                         '1. Change a', '2. Replace b with c', '3. Change c', '4. Add d'])
        self.assertEqual(self.included(), 4)

    def test_per_commit_with_nothing_new_is_refused(self):
        self.update_fails(
            '--per-commit', message='CL 101 already includes the 2 commits since main')

    def test_per_commit_with_a_base_is_refused(self):
        self.commit_change_c()
        self.update_fails('--per-commit', '-b', 'main', message='Leave out -b')

    def test_a_base_above_the_last_included_commit_is_refused(self):
        self.commit_change_c()
        self.commit('Change d')
        self.update_fails(
            '-b', 'HEAD~1', message='-b HEAD~1 skips 1 commit CL 101 doesn\'t include yet')

    def test_a_dry_run_leaves_the_record(self):
        self.commit_change_c()
        self.update('--dry-run')
        self.assertEqual(self.included(), 2)

    def test_a_retry_after_a_failed_shelve_appends_the_new_commits_once(self):
        """The description is written before the files are opened and shelved, and the record after."""
        self.commit_change_c()
        with mock.patch('git_p4son.update.edit_and_shelve', side_effect=CommandError('p4 said no')):
            self.update_fails('--shelve', message='p4 said no')
        self.update('--shelve')
        self.assertEqual(self.p4.pending['101'][3:], [
                         '1. Change a', '2. Replace b with c', '3. Change c'])
        self.assertEqual(self.included(), 3)

    def test_a_retry_after_a_new_commit_appends_only_that_one(self):
        """The user carries on working while the failure is sorted out, then retries."""
        self.commit_change_c()
        with mock.patch('git_p4son.update.edit_and_shelve', side_effect=CommandError('p4 said no')):
            self.update_fails('--shelve', message='p4 said no')
        self.commit('Add d')
        self.update('--shelve')
        self.assertEqual(self.p4.pending['101'][3:],
                         ['1. Change a', '2. Replace b with c', '3. Change c', '4. Add d'])

    def test_no_edit_leaves_the_new_commits_for_the_next_update(self):
        """Counting them without opening their files would leave them out of the CL for good."""
        self.edit('d.txt', 'd1\n')
        self.commit('Add d')
        self.assertIn('[ok] 2 commits included, 1 new, left for an update without --no-edit\n',
                      self.update('--no-edit', '-m', 'Reworded'))
        self.assertEqual(self.p4.pending['101'], [
                         'Reworded'] + FEATURE_DESCRIPTION[1:])
        self.assertEqual(self.included(), 2)
        self.update()
        self.assertEqual(self.p4.pending['101'][-1], '3. Add d')
        self.assertIn('d.txt', self.p4.opened)

    def test_per_commit_with_no_edit_is_refused(self):
        self.commit_change_c()
        self.update_fails('--per-commit', '--no-edit', message='--no-edit')


TestUpdateOnJj = on_jj(TestUpdate)
TestUpdateChecksOnceOnJj = on_jj(TestUpdateChecksOnce)


class TestUpdateIncludedGitRewrites(IncludedTestCase):
    """Rewrites made with git, which jj would see as new changes; TestJjPerCommit makes them with jj."""

    def test_a_squash_of_included_commits_warns_and_still_includes_the_new_one(self):
        """git keeps the first commit's author date, so the squashed commit counts as it, and the second is gone."""
        first_date = author_dates(
            [self.git('rev-parse', 'HEAD~1')], self.ws)[0]
        self.git('reset', '-q', '--soft', 'main')
        self.commit('Change a and replace b with c', date=first_date)
        self.commit_change_c()
        out = self.update()
        self.assertIn(
            'dropped or squashed into another, of the 2 commits CL 101 included: number 2.', out)
        self.assertEqual(self.p4.pending['101'][-1], '3. Change c')
        self.assertEqual(self.included(), 2)

    def test_a_dropped_commit_warns_and_still_includes_the_new_one(self):
        """Dropping Change a would otherwise shift a count onto Change c, which would never be shelved."""
        self.git('rebase', '-q', '--onto', 'main', 'HEAD~1')
        self.commit_change_c()
        out = self.update('--per-commit', '--shelve')
        self.assertEqual(out.count('Gone from the branch'), 1)
        self.assertIn('[ok] 1 commit still included, 1 new\n', out)
        self.assertIn('included: number 1.', out)
        self.assertEqual(self.p4.pending['101'][-1], '3. Change c')
        self.assertEqual(len(self.shelves()), 1)
        self.assertEqual(self.included(), 2)

    def test_a_new_commit_below_included_ones_is_refused(self):
        """Its own patch would undo every included commit above it."""
        self.git('switch', '-q', '-c', 'below', 'main')
        self.edit('x.txt', 'x1\n')
        self.commit('Add x')
        self.git('switch', '-q', 'feature')
        self.git('rebase', '-q', 'below')
        self.update_fails(
            message='"Add x" sits below commits CL 101 already includes')

    def test_a_second_drop_is_numbered_as_the_cl_lists_it(self):
        """The record keeps a dropped commit's slot, as the CL keeps its entry."""
        self.commit_change_c()
        self.update()
        self.git('rebase', '-q', '--onto', 'main', 'HEAD~2')
        self.assertIn('included: number 1.', self.update())
        self.git('reset', '-q', '--hard', 'HEAD~1')
        self.assertIn(
            'dropped or squashed into another, of the 2 commits CL 101 included: number 3.', self.update())
        self.assertEqual(self.p4.pending['101'][3:], [
                         '1. Change a', '2. Replace b with c', '3. Change c'])

    def test_per_commit_with_a_drop_and_nothing_new_warns_once(self):
        self.git('rebase', '-q', '--onto', 'main', 'HEAD~1')
        self.command_fails(update_command, [
                           'update', 'feat', '--per-commit'], message='already includes the 1 commit')
        with captured_output() as out, self.assertRaisesRegex(CommandError, 'already includes'), \
                self.command_args(['update', 'feat', '--per-commit']) as args:
            update_command(args)
        self.assertNotIn('Gone from the branch', out.getvalue())

    def test_an_amend_that_adds_a_file_is_opened_by_the_next_update(self):
        """An amend keeps the commit's identity, so with nothing new update opens every file since the base."""
        self.edit('n.txt', 'n1\n')
        self.git('add', '-A')
        self.git('commit', '-q', '--amend', '--no-edit')
        self.update()
        self.assertIn('n.txt', self.p4.opened)


TestUpdateIncludedOnJj = on_jj(TestUpdateIncluded)
TestUpdatePerCommitOnJj = on_jj(TestUpdatePerCommit)

if __name__ == '__main__':
    unittest.main()
