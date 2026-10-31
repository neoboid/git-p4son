"""Tests for git_p4son.alias module."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from git_p4son.alias import (
    alias_clean_command,
    alias_delete_command,
    alias_list_command,
    alias_new_command,
    alias_show_command,
)
from git_p4son.changelist_store import (
    delete_changelist_alias,
    list_changelist_aliases,
    save_changelist_alias,
)
from git_p4son.common import CommandError, RunError
from tests.helpers import captured_output, cli_args, FakeP4, make_run_result, WorkspaceTestCase


ALIASES = [('feature-a', '100'), ('feature-b', '200'), ('feature-c', '300')]


class TestAliasShowCommand(unittest.TestCase):
    @mock.patch('git_p4son.alias.load_changelist_alias', return_value='100')
    def test_prints_only_the_changelist(self, mock_load):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = alias_show_command(
                cli_args(['alias', 'show', 'feature-a']))
        self.assertEqual(rc, 0)
        self.assertEqual(out.getvalue(), '100\n')
        mock_load.assert_called_once_with('feature-a', '/ws')

    def test_missing_alias_fails(self):
        with tempfile.TemporaryDirectory() as ws, \
                contextlib.redirect_stdout(io.StringIO()) as out:
            with self.assertRaisesRegex(CommandError, 'No changelist alias found'):
                alias_show_command(
                    cli_args(['alias', 'show', 'nope'], workspace_dir=ws))
        self.assertEqual(out.getvalue(), '')


@mock.patch('git_p4son.alias.delete_changelist_alias', return_value=True)
@mock.patch('git_p4son.alias.list_changelist_aliases', return_value=ALIASES)
class TestAliasDeleteCommand(unittest.TestCase):
    def test_deletes_named_alias(self, mock_list, mock_delete):
        rc = alias_delete_command(
            cli_args(['alias', 'delete', 'feature-b']))
        self.assertEqual(rc, 0)
        mock_delete.assert_called_once_with('feature-b', '/ws')
        mock_list.assert_not_called()

    def test_all_deletes_everything_without_prompting(
            self, _list, mock_delete):
        with mock.patch('builtins.input') as mock_input:
            rc = alias_delete_command(
                cli_args(['alias', 'delete', '--all']))
        self.assertEqual(rc, 0)
        mock_input.assert_not_called()
        self.assertEqual(
            [c.args[0] for c in mock_delete.call_args_list],
            ['feature-a', 'feature-b', 'feature-c'])

    def test_all_goes_on_past_a_failed_delete(self, _list, mock_delete):
        mock_delete.side_effect = [None, PermissionError('boom'), None]
        with captured_output() as out:
            rc = alias_delete_command(
                cli_args(['alias', 'delete', '--all']))
        self.assertEqual(rc, 0)
        self.assertEqual(mock_delete.call_count, 3)
        self.assertIn('[err] boom\n', out.getvalue())

    def test_all_without_aliases(self, mock_list, mock_delete):
        mock_list.return_value = []
        rc = alias_delete_command(
            cli_args(['alias', 'delete', '--all']))
        self.assertEqual(rc, 0)
        mock_delete.assert_not_called()


class TestAliasNewAndList(WorkspaceTestCase):
    """alias new and alias list against a real alias store."""

    def _new(self, changelist, alias, force=False):
        return alias_new_command(cli_args(
            ['alias', 'new', changelist, alias] + (['-f'] if force else []),
            workspace_dir=self.ws))

    def _list(self):
        with captured_output() as out:
            alias_list_command(
                cli_args(['alias', 'list'], workspace_dir=self.ws))
        heading, *lines = out.getvalue().lstrip('\n').splitlines()
        self.assertEqual(heading, '# Listing aliases')
        return lines

    def test_new_aliases_are_listed_in_name_order(self):
        self.assertEqual(self._new('200', 'feature-b'), 0)
        self.assertEqual(self._new('100', 'feature-a'), 0)
        self.assertEqual(self._list(), ['Found 2 aliases',
                                        'feature-a -> CL 100',
                                        'feature-b -> CL 200'])

    def test_no_aliases(self):
        self.assertEqual(self._list(), ['No aliases defined'])

    def test_empty_alias_files_are_left_out(self):
        self._new('100', 'feature-a')
        open(os.path.join(self.ws, '.git-p4son', 'aliases', 'empty.toml'),
             'w').close()
        os.makedirs(os.path.join(self.ws, '.git-p4son', 'changelists'))
        open(os.path.join(self.ws, '.git-p4son', 'changelists', 'old-empty'),
             'w').close()
        self.assertEqual(self._list(), ['Found 1 alias',
                                        'feature-a -> CL 100'])

    def test_new_refuses_a_non_numeric_changelist(self):
        with self.assertRaisesRegex(CommandError,
                                    'Invalid changelist number: abc'):
            self._new('abc', 'feature-a')
        self.assertEqual(self._list(), ['No aliases defined'])


class TestAliasClean(WorkspaceTestCase):
    """alias clean against a real alias store and a scripted Perforce server.

    CL 101 is pending, 102 was submitted and 103 doesn't exist, as after a delete or a submit that renumbered it."""

    def setUp(self):
        super().setUp()
        self.p4 = FakeP4(self.ws)
        self.p4.install(self)
        self.p4.pending['101'] = ['Pending']
        self.p4.submit(102, {})
        for name, changelist in (('pending', '101'), ('submitted', '102'),
                                 ('deleted', '103')):
            save_changelist_alias(name, changelist, self.ws, force=False)

    def _aliases(self):
        return list_changelist_aliases(self.ws)

    def _report(self, out):
        """What alias clean printed from its first heading on."""
        return out[out.index('# '):]

    def test_deletes_the_aliases_whose_changelist_is_not_pending(self):
        out = self.run_command(alias_clean_command, ['alias', 'clean'])
        self.assertEqual(self._aliases(), [('pending', '101')])
        self.assertEqual(self._report(out), '# deleted -> CL 103\n[ok] Deleted, CL 103 is not pending\n'
                         '\n# pending -> CL 101\n[ok] Kept, CL 101 is pending\n'
                         '\n# submitted -> CL 102\n[ok] Deleted, CL 102 is not pending\n')

    def test_dry_run_deletes_nothing(self):
        out = self.run_command(alias_clean_command, [
                               'alias', 'clean', '--dry-run'])
        self.assertEqual(len(self._aliases()), 3)
        self.assertEqual(self._report(out), '# deleted -> CL 103\n[ok] Would delete, CL 103 is not pending\n'
                         '\n# pending -> CL 101\n[ok] Would keep, CL 101 is pending\n'
                         '\n# submitted -> CL 102\n[ok] Would delete, CL 102 is not pending\n')

    def test_porcelain_prints_one_line_per_alias(self):
        self.assertEqual(self.p4son('alias', 'clean', '--porcelain'),
                         'deleted deleted 103\nkept pending 101\ndeleted submitted 102\n')

    def test_porcelain_dry_run(self):
        self.assertEqual(self.p4son('alias', 'clean', '--porcelain', '-n'),
                         'would-delete deleted 103\nwould-keep pending 101\nwould-delete submitted 102\n')
        self.assertEqual(len(self._aliases()), 3)

    def test_porcelain_failed_delete(self):
        with mock.patch('git_p4son.alias.delete_changelist_alias',
                        side_effect=[PermissionError('boom'), None]):
            out = self.p4son('alias', 'clean', '--porcelain')
        # captured_output takes stderr too, where the error goes.
        self.assertEqual(
            out, '[err] boom\nfailed deleted 103\nkept pending 101\ndeleted submitted 102\n')

    def test_porcelain_unclear_answer_reaches_the_error(self):
        fake_run = self.p4.run

        def run(command, **kwargs):
            if command[-1] == '101':
                return make_run_result(stderr=['101 - something unexpected.'])
            return fake_run(command, **kwargs)
        with mock.patch('git_p4son.perforce.run', side_effect=run):
            out = self.p4son_fails('alias', 'clean', '--porcelain',
                                   message='CL 101 is pending\n101 - something unexpected.')
        self.assertEqual(out, 'deleted deleted 103\n')

    def test_goes_on_past_a_failed_delete(self):
        with mock.patch('git_p4son.alias.delete_changelist_alias',
                        side_effect=[PermissionError('boom'), None]) as mock_delete:
            out = self.run_command(alias_clean_command, ['alias', 'clean'])
        self.assertEqual([c.args[0] for c in mock_delete.call_args_list],
                         ['deleted', 'submitted'])
        self.assertIn('# deleted -> CL 103\n[err] boom\n', out)
        self.assertIn(
            '# submitted -> CL 102\n[ok] Deleted, CL 102 is not pending\n', out)

    def test_a_failed_lookup_stops_at_its_alias(self):
        fake_run = self.p4.run

        def run(command, **kwargs):
            if command[-1] == '101':
                raise RunError('p4 describe failed', 1, [])
            return fake_run(command, **kwargs)
        with mock.patch('git_p4son.perforce.run', side_effect=run):
            with self.assertRaises(RunError):
                self.run_command(alias_clean_command, ['alias', 'clean'])
        self.assertEqual(self._aliases(), [
                         ('pending', '101'), ('submitted', '102')])

    def test_looks_up_a_changelist_shared_by_aliases_once(self):
        save_changelist_alias('submitted-too', '102', self.ws, force=False)
        out = self.run_command(alias_clean_command, ['alias', 'clean'])
        self.assertEqual([call[-1]
                         for call in self.p4.calls], ['103', '101', '102'])
        self.assertIn(
            '# submitted-too -> CL 102\n[ok] Deleted, CL 102 is not pending\n', out)
        self.assertEqual(self._aliases(), [('pending', '101')])

    def test_all_pending_deletes_nothing(self):
        delete_changelist_alias('submitted', self.ws)
        delete_changelist_alias('deleted', self.ws)
        out = self.run_command(alias_clean_command, ['alias', 'clean'])
        self.assertEqual(self._aliases(), [('pending', '101')])
        self.assertNotIn('Deleted', out)

    def test_no_aliases_asks_perforce_nothing(self):
        for name, _changelist in self._aliases():
            delete_changelist_alias(name, self.ws)
        out = self.run_command(alias_clean_command, ['alias', 'clean'])
        self.assertIn('No changelist aliases to clean', out)
        self.assertEqual(self.p4.calls, [])


class TestAliasCleanMigrates(WorkspaceTestCase):
    """alias clean against a real alias store with aliases in the old one, CL 100 pending and CL 200 gone."""

    def setUp(self):
        super().setUp()
        self.p4 = FakeP4(self.ws)
        self.p4.install(self)
        self.p4.pending['100'] = ['Pending']
        self.old_dir = os.path.join(self.ws, '.git-p4son', 'changelists')
        os.makedirs(self.old_dir)
        for name, changelist in (('feature-a', '100'), ('feature-b', '200')):
            with open(os.path.join(self.old_dir, name), 'w') as f:
                f.write(changelist + '\n')

    def _new_store(self):
        return sorted(os.listdir(
            os.path.join(self.ws, '.git-p4son', 'aliases')))

    def test_clean_moves_the_old_aliases_then_deletes_from_the_new_store(self):
        out = self.run_command(alias_clean_command, ['alias', 'clean'])
        self.assertIn(
            '# Moving old aliases\n[ok] Moved 2 aliases to .git-p4son/aliases/\n', out)
        self.assertLess(out.index('Moved 2 aliases'),
                        out.index('# feature-a -> CL 100'))
        self.assertFalse(os.path.exists(self.old_dir))
        self.assertEqual(self._new_store(), ['feature-a.toml'])

    def test_dry_run_moves_nothing(self):
        out = self.run_command(alias_clean_command, [
                               'alias', 'clean', '--dry-run'])
        self.assertNotIn('Moved', out)
        self.assertEqual(sorted(os.listdir(self.old_dir)),
                         ['feature-a', 'feature-b'])


if __name__ == '__main__':
    unittest.main()
