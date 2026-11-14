"""Tests for git_p4son.update module."""

import shlex
import unittest
from unittest import mock

from git_p4son.changelist_store import find_record, Record, save_changelist_alias, set_review_tool
from git_p4son.common import CommandError
from git_p4son.git import author_dates
from git_p4son.git_backend import GitBackend
from git_p4son.new import new_command
from git_p4son.review_tools import SWARM_SHELF_DELAY
from git_p4son.update import _step, update_command
from tests.helpers import (
    CHECK_REVISIONS,
    OUT_OF_DATE,
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
        return self.run_command(update_command, ['update', '101', *argv])

    def update_fails(self, *argv, message):
        return self.command_fails(update_command, ['update', '101', *argv],
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
        out = self.run_command(update_command, ['update', 'feat'])
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


class TestUpdateOwnPending(UpdateTestCase):
    """update refuses a CL it can't edit before touching it, as alias new does."""

    def setUp(self):
        super().setUp()
        self.commit_change_c()

    def test_refuses_another_users_changelist_before_changing_it(self):
        self.p4.owners['101'] = 'bob'
        self.update_fails('--shelve', message="^CL 101 belongs to bob, not me. If it is a Swarm review's copy, use "
                          'your own CL instead, often the number just before it.$')
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)
        self.assertNotIn(['change', '-i'], self.p4.calls)

    def test_reports_the_check_under_a_heading_of_its_own(self):
        self.assertIn('# Checking CL 101\n', self.update())

    def test_refuses_another_clients_changelist(self):
        self.p4.clients['101'] = 'otherws'
        self.update_fails(message="^CL 101 belongs to client otherws, not myclient, so update can't open files in "
                          'it.$')
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)

    def test_refuses_a_changelist_that_does_not_exist(self):
        self.command_fails(
            update_command, ['update', '999'], message='^CL 999 does not exist.$')

    def test_a_per_commit_update_checks_once_not_in_its_steps(self):
        start_patch(self, 'git_p4son.per_commit.time.sleep')
        self.edit('d.txt', 'd1\n')
        self.commit('Add d')
        self.run_command(update_command, ['update', '101', '--per-commit'])
        self.assertEqual(sum(call[:2] == ['-ztag', 'describe']
                         for call in self.p4.calls), 1)


class TestUpdateUnopened(UpdateTestCase):
    """update stops before shelving when p4 doesn't open a file of the change."""

    def test_an_ignored_file_stops_the_shelve(self):
        self.write_file('gen.txt', 'g\n')
        self.commit('Add gen')
        self.p4.ignored.add('gen.txt')
        out = self.update_fails(
            '--shelve', message='^p4 did not open 1 of 1 file in CL 101')
        self.assertEqual(self.p4.shelved, {})
        # p4 also prints "opened for add" for the file it then refuses, which p4 said leaves out.
        said = out[out.index('p4 said:\n'):]
        self.assertIn("gen.txt - ignored file can't be added.\n", said)
        self.assertNotIn('opened for add', said)

    def test_a_per_commit_step_points_at_resuming_the_run(self):
        start_patch(self, 'git_p4son.per_commit.time.sleep')
        self.write_file('gen.txt', 'g\n')
        self.commit('Add gen')
        self.commit_change_c()
        self.p4.ignored.add('gen.txt')
        self.update_fails('--per-commit', '--shelve',
                          message=r'p4 did not open 1 of 1 file in CL 101\. Leave .*\.p4ignore so p4 takes them\.\n'
                          r'After fixing the problem, resume with: git p4son update 101 --shelve --per-commit$')
        self.assertEqual(self.p4.shelved, {})


class TestSteps(unittest.TestCase):
    def commands(self, **overrides):
        return shlex.join(_step('100', _args(**overrides)))

    def test_plain_update(self):
        self.assertEqual(self.commands(), 'update 100')

    def test_flags(self):
        self.assertEqual(self.commands(no_commit_list=True, no_edit=True, shelve=True, ignore_out_of_date=True),
                         'update 100 --no-commit-list --no-edit --shelve --ignore-out-of-date')


class TestUpdatePerCommit(UpdateTestCase):
    """update --per-commit of CL 101 over the two commits on branch feature, its alias without a record."""

    def setUp(self):
        super().setUp()
        save_changelist_alias('feat', '101', self.ws, force=True)
        self.sleep = start_patch(self, 'git_p4son.per_commit.time.sleep')
        self.feature_head = self.git('rev-parse', 'HEAD')
        self.p4.calls.clear()

    def update(self, *argv):
        return super().update('--per-commit', '-b', 'main', *argv)

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

    def test_a_swarm_review_sleeps_between_shelves_and_runs_no_tool(self):
        set_review_tool('feat', 'swarm', self.ws)
        self.update('--shelve')
        self.assertEqual(len(self.shelves()), 2)
        self.sleep.assert_called_once_with(SWARM_SHELF_DELAY)

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
        self.assertRegex(out, r'Step 1 of 2: \w+ Change a\nStep 2 of 2: \w+ Replace b with c\n'
                         r'\[ok\] Would run 2 steps, git p4son update 101\n')
        # Only the reads that check the CL is the user's.
        self.assertEqual(
            self.p4.calls, [['-ztag', 'describe', '-s', '-m1', '101'], ['-ztag', 'info']])


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
        return self.command_fails(update_command, ['update', 'feat', *argv], message)

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

    def test_per_commit_refuses_a_base(self):
        self.commit_change_c()
        self.update_fails('--per-commit', '-b', 'main',
                          message='Leave out -b main')
        self.assertEqual(self.included(), 2)

    def test_a_base_other_than_the_recorded_one_is_refused(self):
        self.commit_change_c()
        for base in ('main', *self.git('rev-list', 'main..HEAD').split()):
            with self.subTest(base=base):
                self.command_fails(update_command, ['update', '101', '-b', base],
                                   message=f'Leave out -b {base}')
        self.assertEqual(self.included(), 2)

    def test_a_failed_per_commit_step_resumes_without_a_base(self):
        self.commit_change_c()
        self.commit('Change d')
        calls = []
        real = update_command

        def fail_second(args):
            calls.append(args)
            if len(calls) == 2:
                raise CommandError('p4 said no')
            return real(args)
        start_patch(self, 'git_p4son.cli.update_command',
                    side_effect=fail_second)
        self.command_fails(update_command, ['update', '101', '--per-commit'],
                           message=r'resume with: git p4son update 101 --per-commit$')
        self.update('--per-commit')
        self.assertEqual(self.included(), 4)

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


class TestUpdateOnAnOldSync(UpdateTestCase):
    """update on branch feature, after main synced past the commit it forked from."""

    def build_workspace(self):
        super().build_workspace()
        self.commit_change_c()
        self.commit_sync_on_main(102, {'d.txt': 'd1\n'})

    def test_update_is_refused_before_touching_the_changelist(self):
        self.p4.calls.clear()
        for argv in ([], ['--dry-run'], ['-b', 'main'], ['--shelve']):
            with self.subTest(argv=argv):
                self.p4son_fails('update', '101', *argv,
                                 message='not based on the latest main')
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)
        self.assertEqual(self.p4.calls, [])

    def test_no_edit_opens_nothing_so_it_runs(self):
        self.p4son('update', '101', '--no-edit', '-m', 'Reworded')
        self.assertEqual(self.p4.pending['101'][0], 'Reworded')
        self.assertEqual(self.p4.opened, FEATURE_OPENED)

    def test_rebased_onto_the_sync_branch_it_runs(self):
        self.git('rebase', '-q', 'main', 'feature')
        self.p4son('update', '101')
        self.assertEqual(self.p4.pending['101'][-1], '3. Change c')

    def test_per_commit_below_a_merge_of_the_sync_branch_is_refused(self):
        self.git('merge', '-q', '--no-edit', 'main')
        self.p4.calls.clear()
        self.p4son_fails('update', '101', '--per-commit', '--shelve',
                         message='Commit [0-9a-f]{7} is not based on the latest main')
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)
        self.assertEqual(self.shelves(), [])


TestUpdateOnJj = on_jj(TestUpdate)
TestUpdateOnAnOldSyncOnJj = on_jj(TestUpdateOnAnOldSync, skip={
    'test_per_commit_below_a_merge_of_the_sync_branch_is_refused':
    'update takes -b for a jj change with several parents, and refuses it for a changelist with a record'})
TestUpdateChecksOnceOnJj = on_jj(TestUpdateChecksOnce)


class TestUpdateOutOfDate(IncludedTestCase):
    """update of CL 101 on files submitted again since the workspace's last sync."""

    def test_shelving_refuses_a_file_only_the_included_commits_opened(self):
        self.commit_change_c()
        self.p4.submit(102, {'a.txt': 'a3\n'})
        for argv in (['--shelve'], ['--shelve', '--dry-run']):
            with self.subTest(argv=argv):
                out = self.update_fails(*argv, message=OUT_OF_DATE)
                self.assertIn('a.txt (have #1, head #2)', out)
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)
        self.assertEqual(self.p4.shelved, {})
        self.assertEqual(self.included(), 2)

    def test_shelving_refuses_a_file_a_new_commit_adds(self):
        self.edit('d.txt', 'd1\n')
        self.commit('Add d')
        self.p4.submit(102, {'d.txt': 'theirs\n'})
        out = self.update_fails('--shelve', message=OUT_OF_DATE)
        self.assertIn('d.txt (not synced, head #1)', out)

    def test_no_edit_checks_only_the_files_the_changelist_has_opened(self):
        self.edit('d.txt', 'd1\n')
        self.commit('Add d')
        self.p4.submit(102, {'d.txt': 'theirs\n'})
        out = self.update('--no-edit', '--shelve')
        self.assertIn('3 files at the latest revision', out)
        self.p4.submit(103, {'a.txt': 'a3\n'})
        out = self.update_fails('--no-edit', '--shelve', message=OUT_OF_DATE)
        self.assertIn('a.txt (have #1, head #2)', out)
        self.assertNotIn('d.txt', out.split(CHECK_REVISIONS)[1])

    def test_without_shelving_there_is_no_check(self):
        self.commit_change_c()
        self.p4.submit(102, {'a.txt': 'a3\n'})
        out = self.update()
        self.assertNotIn(CHECK_REVISIONS, out)
        self.assertEqual(self.p4.pending['101'][-1], '3. Change c')

    def test_ignore_out_of_date_shelves_anyway(self):
        self.commit_change_c()
        self.p4.submit(102, {'a.txt': 'a3\n'})
        out = self.update('--shelve', '--ignore-out-of-date')
        self.assertNotIn(CHECK_REVISIONS, out)
        self.assertEqual(self.p4.shelved['101']['c.txt'], 'c2\n')

    def test_per_commit_checks_once_not_in_its_steps(self):
        self.commit_change_c()
        self.edit('d.txt', 'd1\n')
        self.commit('Add d')
        out = self.update('--shelve', '--per-commit')
        self.assertEqual(out.count(CHECK_REVISIONS), 1)
        self.assertEqual(len(self.shelves()), 2)

    def test_per_commit_refuses_before_the_first_step(self):
        self.commit_change_c()
        self.p4.submit(102, {'a.txt': 'a3\n'})
        out = self.update_fails(
            '--shelve', '--per-commit', message=OUT_OF_DATE)
        self.assertNotIn('Step 1', out)
        self.assertEqual(self.p4.shelved, {})


class TestUpdateOutOfDateWithoutRecord(UpdateTestCase):
    """update --shelve of CL 101, whose alias records no commits, so the files since -b are the change's."""

    def test_shelving_refuses_a_file_behind_the_head(self):
        save_changelist_alias('feat', '101', self.ws, force=True)
        self.p4.submit(102, {'a.txt': 'a3\n'})
        out = self.update_fails('--shelve', message=OUT_OF_DATE)
        self.assertIn('a.txt (have #1, head #2)', out)
        self.assertEqual(self.p4.shelved, {})


class TestUpdateLostBase(P4RepoTestCase):
    """update of CL 101, whose alias records a base branch that was deleted after new."""

    def build_workspace(self):
        super().build_workspace()
        self.git('branch', 'base-b', 'main')
        self.commit_feature()
        self.run_command(
            new_command, ['new', 'feat', '-m', 'Feature', '-b', 'base-b'])
        self.git('branch', '-D', 'base-b')

    def test_update_refuses_and_names_the_fix(self):
        self.command_fails(update_command, ['update', 'feat'],
                           message=r'base-b.*git p4son update feat -b <base>')

    def test_another_resolve_error_is_not_taken_for_a_lost_base(self):
        resolve_base = GitBackend.resolve_base

        def conflicted_at_the_recorded_base(backend, base):
            # Such as jj's check for conflicts up to the tip, which resolve_base runs too.
            if base == 'base-b':
                raise CommandError('The selected jj changes have conflicts')
            return resolve_base(backend, base)
        with mock.patch.object(GitBackend, 'resolve_base', autospec=True, side_effect=conflicted_at_the_recorded_base):
            for argv in ([], ['-b', 'main']):
                with self.subTest(argv=argv):
                    self.command_fails(
                        update_command, ['update', 'feat', *argv], message='^The selected jj changes')
        self.assertEqual(recorded('101', self.ws).base, 'base-b')

    def test_per_commit_with_b_records_the_new_base_before_its_steps(self):
        self.edit('c.txt', 'c2\n')
        self.commit('Change c')
        self.edit('c.txt', 'c3\n')
        self.commit('Change c again')
        self.run_command(update_command, [
                         'update', 'feat', '--per-commit', '-b', 'main'])
        self.assertEqual(recorded('101', self.ws).base, 'main')
        self.assertEqual(self.p4.pending['101'][-2:], [
                         '3. Change c', '4. Change c again'])

    def test_update_with_b_records_the_new_base(self):
        self.edit('c.txt', 'c2\n')
        self.commit('Change c')
        self.run_command(update_command, ['update', 'feat', '-b', 'main'])
        self.assertEqual(recorded('101', self.ws).base, 'main')
        self.assertEqual(self.p4.pending['101'][-1], '3. Change c')


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

    def amend_adding_n(self):
        """Amend the last commit with a new file, n.txt, keeping its identity."""
        self.edit('n.txt', 'n1\n')
        self.git('add', '-A')
        self.git('commit', '-q', '--amend', '--no-edit')

    def test_an_amend_that_adds_a_file_is_opened_by_the_next_update(self):
        """An amend keeps the commit's identity, and update opens every file since the base."""
        self.amend_adding_n()
        self.update()
        self.assertIn('n.txt', self.p4.opened)

    def test_an_amend_is_opened_with_a_new_commit_after_it(self):
        self.amend_adding_n()
        self.commit_change_c()
        self.update()
        self.assertIn('n.txt', self.p4.opened)
        self.assertIn('c.txt', self.p4.opened)


TestUpdateIncludedOnJj = on_jj(TestUpdateIncluded)
TestUpdatePerCommitOnJj = on_jj(TestUpdatePerCommit)
TestUpdateOutOfDateOnJj = on_jj(TestUpdateOutOfDate)

if __name__ == '__main__':
    unittest.main()
