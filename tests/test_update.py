"""Tests for git_p4son.update module."""

import shlex
import unittest

from git_p4son.changelist_store import save_changelist_alias
from git_p4son.common import CommandError
from git_p4son.new import new_command
from git_p4son.update import _step, update_command
from tests.helpers import (
    captured_output,
    cli_args,
    GitRepoTestCase,
    P4RepoTestCase,
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

    def test_no_edit_skips_the_check_and_opens_nothing(self):
        self.edit('d.txt', 'd1\n')
        self.commit('Add d')
        self.edit('a.txt', 'dirty\n')
        self.update('--no-edit')
        self.assertEqual(self.p4.pending['101'][-1], '3. Add d')
        self.assertNotIn('d.txt', self.p4.opened)

    def test_no_edit_shelves_without_reverting(self):
        self.edit('a.txt', 'a1\n')
        self.commit('Restore a')
        self.update('--no-edit', '--shelve')
        self.assertEqual(self.p4.opened, FEATURE_OPENED)
        self.assertEqual(set(self.p4.shelved['101']),
                         {'a.txt', 'b.txt', 'c.txt'})


class TestSteps(unittest.TestCase):
    def commands(self, **overrides):
        return shlex.join(_step('100', _args(**overrides)))

    def test_plain_update(self):
        self.assertEqual(self.commands(), 'update 100')

    def test_flags(self):
        self.assertEqual(self.commands(no_commit_list=True, no_edit=True, shelve=True),
                         'update 100 --no-commit-list --no-edit --shelve')


class TestUpdatePerCommit(UpdateTestCase):
    """update --per-commit of CL 101 over the two commits on branch feature."""

    def setUp(self):
        super().setUp()
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

    def test_relative_base_is_resolved_where_the_command_starts(self):
        """HEAD~2 is main for every step, not the commit two before each step's commit."""
        self.run_command(update_command, [
                         'update', 'feat', '--per-commit', '-b', 'HEAD~2'])
        self.assertEqual(self.p4.pending['111'], FEATURE_DESCRIPTION)
        self.assertEqual(set(self.p4.opened), {'a.txt', 'b.txt', 'c.txt'})


if __name__ == '__main__':
    unittest.main()
