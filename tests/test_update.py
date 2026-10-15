"""Tests for git_p4son.update module."""

import unittest
from unittest import mock

from git_p4son.common import CommandError
from git_p4son.new import new_command
from git_p4son.update import _step_command, update_command
from tests.helpers import cli_args, P4RepoTestCase


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

    def setUp(self):
        super().setUp()
        self.commit_feature()
        self.run_command(
            new_command, ['new', 'feat', '-m', 'Feature', '-b', 'main'])
        self.p4.calls.clear()

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


COMMITS = ['abc1234 First commit', 'def5678 Second commit']


class TestStepCommand(unittest.TestCase):
    def test_plain_update(self):
        self.assertEqual(_step_command('100', _args()), 'update 100')

    def test_flags(self):
        self.assertEqual(
            _step_command('100', _args(no_commit_list=True, no_edit=True,
                                       shelve=True)),
            'update 100 --no-commit-list --no-edit --shelve')


@mock.patch('git_p4son.rebase_todo.check_git_workspace_clean', mock.Mock())
@mock.patch('git_p4son.rebase_todo._run_todo_rebase', return_value=0)
@mock.patch('git_p4son.rebase_todo.get_commit_lines_since', return_value=COMMITS)
@mock.patch('git_p4son.update.update_changelist')
class TestUpdatePerCommit(unittest.TestCase):
    def test_runs_rebase_without_editor(self, mock_update,
                                        mock_commits, mock_rebase):
        rc = update_command(_args(per_commit=True, base_branch='HEAD~2',
                                  shelve=True))
        self.assertEqual(rc, 0)
        mock_update.assert_not_called()
        mock_commits.assert_called_once_with('HEAD~2', '/ws')
        todo, base, ws = mock_rebase.call_args.args
        self.assertEqual((base, ws), ('HEAD~2', '/ws'))
        self.assertFalse(mock_rebase.call_args.kwargs['edit_todo'])
        self.assertIn('exec git p4son update 100 --shelve', todo)

    @mock.patch('git_p4son.update.load_changelist_alias', return_value='200')
    def test_alias_resolved_to_changelist(self, _load, _update,
                                          _commits, mock_rebase):
        rc = update_command(_args(per_commit=True, changelist='feat'))
        self.assertEqual(rc, 0)
        self.assertIn('exec git p4son update 200',
                      mock_rebase.call_args.args[0])

    def test_sleeps_between_steps_only_when_shelving(self, _update, _commits,
                                                     mock_rebase):
        update_command(_args(per_commit=True, shelve=True))
        self.assertIn('--sleep 5', mock_rebase.call_args.args[0])
        update_command(_args(per_commit=True))
        self.assertNotIn('--sleep', mock_rebase.call_args.args[0])

    def test_no_commits(self, _update, mock_commits, mock_rebase):
        mock_commits.return_value = []
        with self.assertRaisesRegex(CommandError, 'No commits found'):
            update_command(_args(per_commit=True))
        mock_rebase.assert_not_called()

    def test_dry_run_passed_to_the_rebase(self, _update, _commits,
                                          mock_rebase):
        rc = update_command(_args(per_commit=True, dry_run=True))
        self.assertEqual(rc, 0)
        self.assertTrue(mock_rebase.call_args.kwargs['dry_run'])

    def test_message_passed_to_the_rebase(self, _update, _commits,
                                          mock_rebase):
        rc = update_command(_args(per_commit=True, message='One\nTwo'))
        self.assertEqual(rc, 0)
        self.assertEqual(mock_rebase.call_args.kwargs['message'], 'One\nTwo')


if __name__ == '__main__':
    unittest.main()
