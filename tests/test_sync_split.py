"""Tests for git_p4son.sync_split module."""

import argparse
import unittest
from unittest import mock

from git_p4son.depot import ResolvedDepot
from git_p4son.sync import LastSync, sync_preflight
from git_p4son.sync_split import sync_split_command
from tests.helpers import make_changes, make_run_result


class TestSyncSplitCommand(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch(
            'git_p4son.sync_split.resolve_depot_root',
            return_value=ResolvedDepot(depot_root='//myclient',
                                       client_spec=None))
        patcher.start()
        self.addCleanup(patcher.stop)
        # The gate has its own tests; here it must simply not shell out.
        gate = mock.patch('git_p4son.sync_split.sync_preflight',
                          return_value=True)
        gate.start()
        self.addCleanup(gate.stop)

    def _args(self, **overrides):
        values = dict(changelist=None, user=None, dry_run=False,
                      workspace_dir='/ws')
        values.update(overrides)
        return mock.Mock(**values)

    @mock.patch('git_p4son.sync_split.sync_command', return_value=0)
    @mock.patch('git_p4son.sync_split.get_submitted_changes')
    @mock.patch('git_p4son.sync_split.get_p4_user', return_value='me')
    @mock.patch('git_p4son.sync_split.get_latest_changelist', return_value=106)
    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    def test_delegates_resolved_sequence_to_sync(
            self, _last_sync, _latest, _user, mock_changes, mock_sync):
        mock_changes.return_value = make_changes(
            (100, 'other'), (101, 'other'), (102, 'me'),
            (103, 'other'), (104, 'other'), (105, 'me'), (106, 'other'))
        args = self._args()
        rc = sync_split_command(args)
        self.assertEqual(rc, 0)
        mock_sync.assert_called_once_with(args)
        self.assertEqual(args.changelist, ['101', '102', '104', '105', '106'])
        self.assertFalse(args.force)
        # The sequence already splits out the given users, so sync must not
        # split out the configured ones on top of it.
        self.assertTrue(args.no_split)

    @mock.patch('git_p4son.sync_split.sync_command', return_value=0)
    @mock.patch('git_p4son.sync_split.get_submitted_changes')
    @mock.patch('git_p4son.sync_split.get_p4_user', return_value='me')
    @mock.patch('git_p4son.sync_split.get_latest_changelist')
    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    def test_explicit_upper_bound_skips_head_lookup(
            self, _last_sync, mock_latest, _user, mock_changes, mock_sync):
        mock_changes.return_value = make_changes(
            (100, 'other'), (101, 'other'), (102, 'me'))
        args = self._args(changelist='103')
        rc = sync_split_command(args)
        self.assertEqual(rc, 0)
        mock_latest.assert_not_called()
        mock_changes.assert_called_once_with('//myclient', 100, 103, '/ws')
        self.assertEqual(args.changelist, ['101', '102', '103'])

    @mock.patch('git_p4son.sync_split.sync_command', return_value=0)
    @mock.patch('git_p4son.sync_split.get_submitted_changes')
    @mock.patch('git_p4son.sync_split.get_p4_user')
    @mock.patch('git_p4son.sync_split.get_latest_changelist', return_value=103)
    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    def test_user_option_overrides_p4_user(
            self, _last_sync, _latest, mock_p4_user, mock_changes, _sync):
        mock_changes.return_value = make_changes(
            (100, 'other'), (101, 'other'), (102, 'colleague'))
        args = self._args(user=['colleague'])
        rc = sync_split_command(args)
        self.assertEqual(rc, 0)
        mock_p4_user.assert_not_called()
        self.assertEqual(args.changelist, ['101', '102', '103'])

    @mock.patch('git_p4son.sync_split.sync_command')
    @mock.patch('git_p4son.sync_split.get_submitted_changes')
    @mock.patch('git_p4son.sync_split.get_p4_user', return_value='me')
    @mock.patch('git_p4son.sync_split.get_latest_changelist', return_value=103)
    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    def test_dry_run_does_not_sync(
            self, _last_sync, _latest, _user, mock_changes, mock_sync):
        mock_changes.return_value = make_changes(
            (100, 'other'), (101, 'other'), (102, 'me'))
        rc = sync_split_command(self._args(dry_run=True))
        self.assertEqual(rc, 0)
        mock_sync.assert_not_called()

    @mock.patch('git_p4son.sync_split.git_last_sync', return_value=None)
    def test_no_previous_sync_aborts(self, _last_sync):
        """Without a synced changelist there is no lower bound for the scan."""
        rc = sync_split_command(self._args())
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync_split.sync_command')
    @mock.patch('git_p4son.sync_split.get_latest_changelist', return_value=100)
    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    def test_already_at_target_is_a_no_op(
            self, _last_sync, _latest, mock_sync):
        rc = sync_split_command(self._args())
        self.assertEqual(rc, 0)
        mock_sync.assert_not_called()

    @mock.patch('git_p4son.sync_split.sync_command')
    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    def test_older_target_aborts(self, _last_sync, mock_sync):
        rc = sync_split_command(self._args(changelist='90'))
        self.assertEqual(rc, 1)
        mock_sync.assert_not_called()

    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    def test_invalid_changelist_aborts(self, _last_sync):
        rc = sync_split_command(self._args(changelist='not-a-number'))
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync_split.get_p4_user', return_value=None)
    @mock.patch('git_p4son.sync_split.get_latest_changelist', return_value=103)
    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    def test_unknown_p4_user_aborts(self, _last_sync, _latest, _user):
        rc = sync_split_command(self._args())
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync_split.resolve_depot_root', return_value=None)
    def test_unresolved_depot_root_aborts(self, _resolved):
        rc = sync_split_command(self._args())
        self.assertEqual(rc, 1)


class TestSyncSplitPreSyncHooks(unittest.TestCase):
    """The hooks must fire before the p4 round trips they exist to save,
    exactly once, and never for a run that syncs nothing."""

    def setUp(self):
        patcher = mock.patch(
            'git_p4son.sync_split.resolve_depot_root',
            return_value=ResolvedDepot(depot_root='//myclient',
                                       client_spec=None))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _args(self, **overrides):
        values = dict(changelist=None, user=None, dry_run=False,
                      workspace_dir='/ws', invocation_dir='/invoked')
        values.update(overrides)
        return argparse.Namespace(**values)

    @mock.patch('git_p4son.sync_split.sync_command', return_value=0)
    @mock.patch('git_p4son.sync_split.get_submitted_changes')
    @mock.patch('git_p4son.sync_split.get_p4_user', return_value='me')
    @mock.patch('git_p4son.sync_split.get_latest_changelist', return_value=103)
    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    @mock.patch('git_p4son.sync_split.sync_preflight', return_value=True)
    def test_hooks_run_before_the_expensive_queries(
            self, mock_hooks, _last_sync, _latest, mock_user, mock_changes,
            _sync):
        """The p4 user and changes lookups must not happen until the hooks
        have had their say."""
        order = []
        mock_hooks.side_effect = lambda *a, **k: order.append('hooks') or True
        mock_user.side_effect = lambda *a: order.append('p4 info') or 'me'
        mock_changes.side_effect = lambda *a: order.append('p4 changes') or []
        rc = sync_split_command(self._args())
        self.assertEqual(rc, 0)
        self.assertEqual(order, ['hooks', 'p4 info', 'p4 changes'])

    @mock.patch('git_p4son.sync_split.sync_command', return_value=0)
    @mock.patch('git_p4son.sync_split.get_submitted_changes', return_value=[])
    @mock.patch('git_p4son.sync_split.get_p4_user', return_value='me')
    @mock.patch('git_p4son.sync_split.get_latest_changelist', return_value=103)
    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    @mock.patch('git_p4son.sync_split.sync_preflight', return_value=True)
    def test_sync_command_is_told_not_to_rerun_them(
            self, _hooks, _last_sync, _latest, _user, _changes, _sync):
        args = self._args()
        sync_split_command(args)
        self.assertTrue(args.preflight_done)

    @mock.patch('git_p4son.sync_split.sync_command', return_value=0)
    @mock.patch('git_p4son.sync_split.get_submitted_changes', return_value=[])
    @mock.patch('git_p4son.sync_split.get_p4_user', return_value='me')
    @mock.patch('git_p4son.sync_split.get_latest_changelist', return_value=103)
    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    @mock.patch('git_p4son.sync_split.sync_preflight', return_value=True)
    def test_sync_command_reuses_the_resolved_depot(
            self, _hooks, _last_sync, _latest, _user, _changes, _sync):
        """Resolving the depot root queries the client spec from the
        server, so sync must not do it a second time."""
        args = self._args()
        sync_split_command(args)
        self.assertEqual(args.resolved_depot,
                         ResolvedDepot(depot_root='//myclient',
                                       client_spec=None))

    @mock.patch('git_p4son.sync_split.get_submitted_changes')
    @mock.patch('git_p4son.sync_split.get_p4_user')
    @mock.patch('git_p4son.sync_split.sync_command')
    @mock.patch('git_p4son.sync_split.get_latest_changelist', return_value=103)
    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    @mock.patch('git_p4son.sync_split.sync_preflight', return_value=False)
    def test_failing_hook_aborts_before_any_query(
            self, _hooks, _last_sync, _latest, mock_sync, mock_user,
            mock_changes):
        rc = sync_split_command(self._args())
        self.assertEqual(rc, 1)
        mock_user.assert_not_called()
        mock_changes.assert_not_called()
        mock_sync.assert_not_called()

    @mock.patch('git_p4son.sync_split.get_submitted_changes', return_value=[])
    @mock.patch('git_p4son.sync_split.get_p4_user', return_value='me')
    @mock.patch('git_p4son.sync_split.get_latest_changelist', return_value=103)
    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    @mock.patch('git_p4son.sync_split.sync_preflight')
    def test_dry_run_does_not_fire_hooks(
            self, mock_hooks, _last_sync, _latest, _user, _changes):
        rc = sync_split_command(self._args(dry_run=True))
        self.assertEqual(rc, 0)
        mock_hooks.assert_not_called()

    @mock.patch('git_p4son.sync_split.get_latest_changelist', return_value=100)
    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    @mock.patch('git_p4son.sync_split.sync_preflight')
    def test_nothing_to_sync_does_not_fire_hooks(
            self, mock_hooks, _last_sync, _latest):
        """Already at the target: sync skips its hooks in that case, and
        so must sync-split."""
        rc = sync_split_command(self._args())
        self.assertEqual(rc, 0)
        mock_hooks.assert_not_called()

    @mock.patch('git_p4son.sync_split.sync_preflight')
    @mock.patch('git_p4son.sync_split.git_last_sync', return_value=None)
    def test_unsynced_workspace_does_not_fire_hooks(
            self, _last_sync, mock_hooks):
        rc = sync_split_command(self._args())
        self.assertEqual(rc, 1)
        mock_hooks.assert_not_called()


class TestSyncPreflight(unittest.TestCase):
    """The one gate both commands share: workspace checks then hooks, in
    that order, and skipped wholesale when a caller already ran it."""

    @mock.patch('git_p4son.sync.run_hooks')
    @mock.patch('git_p4son.sync.p4_get_opened_files')
    @mock.patch('git_p4son.lib.get_dirty_files')
    def test_already_done_skips_everything(
            self, mock_dirty, mock_opened, mock_run_hooks):
        self.assertTrue(sync_preflight('//ws', '/ws', '/invoked', True))
        mock_dirty.assert_not_called()
        mock_opened.assert_not_called()
        mock_run_hooks.assert_not_called()

    @mock.patch('git_p4son.sync.run_hooks', return_value=[])
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.lib.get_dirty_files', return_value=[])
    def test_clean_workspaces_run_the_hooks(
            self, _dirty, _opened, mock_run_hooks):
        self.assertTrue(sync_preflight('//ws', '/ws', '/invoked'))
        mock_run_hooks.assert_called_once_with('pre-sync', '/ws', '/invoked')

    @mock.patch('git_p4son.sync.run_hooks')
    @mock.patch('git_p4son.sync.p4_get_opened_files')
    @mock.patch('git_p4son.lib.get_dirty_files',
                return_value=[('a.txt', 'modify')])
    def test_dirty_git_stops_before_p4_and_hooks(
            self, _dirty, mock_opened, mock_run_hooks):
        self.assertFalse(sync_preflight('//ws', '/ws', '/invoked'))
        mock_opened.assert_not_called()
        mock_run_hooks.assert_not_called()

    @mock.patch('git_p4son.sync.run_hooks')
    @mock.patch('git_p4son.sync.is_file_tracked', return_value=True)
    @mock.patch('git_p4son.sync.p4_get_opened_files',
                return_value=[('a.txt', 'modify')])
    @mock.patch('git_p4son.lib.get_dirty_files', return_value=[])
    def test_dirty_p4_stops_before_hooks(
            self, _dirty, _opened, _tracked, mock_run_hooks):
        self.assertFalse(sync_preflight('//ws', '/ws', '/invoked'))
        mock_run_hooks.assert_not_called()

    @mock.patch('git_p4son.sync.run_hooks',
                return_value=[make_run_result(returncode=1)])
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.lib.get_dirty_files', return_value=[])
    def test_failing_hook_fails_the_gate(self, _dirty, _opened, _hooks):
        self.assertFalse(sync_preflight('//ws', '/ws', '/invoked'))


class TestSyncSplitCommandMultiUser(unittest.TestCase):

    def setUp(self):
        patcher = mock.patch(
            'git_p4son.sync_split.resolve_depot_root',
            return_value=ResolvedDepot(depot_root='//myclient',
                                       client_spec=None))
        patcher.start()
        self.addCleanup(patcher.stop)
        # The gate has its own tests; here it must simply not shell out.
        gate = mock.patch('git_p4son.sync_split.sync_preflight',
                          return_value=True)
        gate.start()
        self.addCleanup(gate.stop)

    def _args(self, **overrides):
        values = dict(changelist=None, user=None, dry_run=False,
                      workspace_dir='/ws')
        values.update(overrides)
        return mock.Mock(**values)

    @mock.patch('git_p4son.sync_split.sync_command', return_value=0)
    @mock.patch('git_p4son.sync_split.get_submitted_changes')
    @mock.patch('git_p4son.sync_split.get_p4_user')
    @mock.patch('git_p4son.sync_split.get_latest_changelist', return_value=106)
    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    def test_multiple_users_are_all_split_out(
            self, _last_sync, _latest, mock_p4_user, mock_changes, _sync):
        mock_changes.return_value = make_changes(
            (100, 'other'), (101, 'other'), (102, 'alice'),
            (103, 'other'), (104, 'other'), (105, 'bob'), (106, 'other'))
        args = self._args(user=['alice', 'bob'])
        rc = sync_split_command(args)
        self.assertEqual(rc, 0)
        mock_p4_user.assert_not_called()
        self.assertEqual(args.changelist, ['101', '102', '104', '105', '106'])

    @mock.patch('git_p4son.sync_split.sync_command', return_value=0)
    @mock.patch('git_p4son.sync_split.get_submitted_changes')
    @mock.patch('git_p4son.sync_split.get_p4_user')
    @mock.patch('git_p4son.sync_split.get_latest_changelist', return_value=103)
    @mock.patch('git_p4son.sync_split.git_last_sync',
                return_value=LastSync(changelist=100, commit='abc123'))
    def test_repeated_user_names_are_collapsed(
            self, _last_sync, _latest, _p4_user, mock_changes, _sync):
        """Naming the same user twice (in any casing) must not change the
        resolved sequence."""
        mock_changes.return_value = make_changes(
            (100, 'other'), (101, 'other'), (102, 'alice'))
        args = self._args(user=['alice', 'Alice'])
        rc = sync_split_command(args)
        self.assertEqual(rc, 0)
        self.assertEqual(args.changelist, ['101', '102', '103'])


if __name__ == '__main__':
    unittest.main()
