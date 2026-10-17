"""Tests for git_p4son.sync module."""

import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

from git_p4son.common import CommandError, RunError
from git_p4son.git import format_sync_subject
from git_p4son.perforce import P4SyncAbortError, P4SyncClobberError
from git_p4son.sync import (
    build_sync_targets,
    _collect_split_users,
    _make_synced_files_writable,
    _should_split,
    _sync_changelist,
    sync_command,
    sync_preflight,
)
from tests.helpers import (
    captured_output,
    cli_args,
    make_changes,
    make_run_result,
    P4RepoTestCase,
)


def _sync_args(changelist, **overrides):
    return cli_args(['sync', *changelist], **overrides)


@mock.patch('git_p4son.writable.make_writable', return_value=0)
@mock.patch('git_p4son.sync.get_tracked_files')
class TestMakeSyncedFilesWritable(unittest.TestCase):
    """Writable mode's post-sync step: tracked synced files go writable."""

    @mock.patch('git_p4son.sync.is_writable_mode', return_value=False)
    def test_does_nothing_when_mode_is_off(self, _mode, mock_tracked,
                                           mock_make):
        _make_synced_files_writable(['/ws/a.cpp'], '/ws')
        mock_tracked.assert_not_called()
        mock_make.assert_not_called()

    @mock.patch('git_p4son.sync.is_writable_mode', return_value=True)
    def test_does_nothing_when_nothing_was_synced(self, _mode, mock_tracked,
                                                  mock_make):
        _make_synced_files_writable([], '/ws')
        mock_tracked.assert_not_called()
        mock_make.assert_not_called()

    @mock.patch('git_p4son.sync.is_writable_mode', return_value=True)
    def test_only_tracked_files_are_made_writable(self, _mode, mock_tracked,
                                                  mock_make):
        """Only tracked files are made writable, each looked up once."""
        mock_tracked.return_value = {'/ws/a.cpp', '/ws/b.h'}
        _make_synced_files_writable(
            ['/ws/b.h', '/ws/a.cpp', '/ws/Content/x.uasset', '/ws/a.cpp'],
            '/ws')
        mock_tracked.assert_called_once_with(
            ['/ws/Content/x.uasset', '/ws/a.cpp', '/ws/b.h'], '/ws')
        mock_make.assert_called_once_with(['/ws/a.cpp', '/ws/b.h'])


@mock.patch('git_p4son.sync.p4_sync')
class TestSyncChangelist(unittest.TestCase):
    """p4 refusing an expected (git-ignored) file is tolerated; any other refusal raises."""

    def _clobber(self, *refused):
        return P4SyncClobberError(
            RunError('p4 sync failed', 1, []), list(refused))

    def test_clean_sync_refuses_nothing(self, mock_p4_sync):
        self.assertEqual(
            _sync_changelist(12345, 'test', '//myclient', '/ws', expected_clobber=set()), [])
        mock_p4_sync.assert_called_once_with(12345, '//myclient', '/ws')

    def test_expected_clobber_tolerated(self, mock_p4_sync):
        mock_p4_sync.side_effect = self._clobber('/ws/a.txt', '/ws/b.txt')
        refused = _sync_changelist(12345, 'test', '//myclient', '/ws',
                                   expected_clobber={'/ws/a.txt', '/ws/b.txt'})
        self.assertEqual(refused, ['/ws/a.txt', '/ws/b.txt'])

    def test_failed_rename_aborts_with_advice(self, mock_p4_sync):
        mock_p4_sync.side_effect = P4SyncAbortError(
            'rename: failed to rename D:\\ws\\a.uasset')
        with captured_output() as out, \
                self.assertRaises(CommandError) as ctx:
            _sync_changelist(12345, 'test', '//myclient',
                             '/ws', expected_clobber=set())
        self.assertIn('\nrename: failed to rename D:\\ws\\a.uasset\n',
                      out.getvalue())
        self.assertIn('partially synced', str(ctx.exception))

    def test_unexpected_clobber_raises(self, mock_p4_sync):
        mock_p4_sync.side_effect = self._clobber('/ws/a.txt')
        with captured_output() as out, \
                self.assertRaisesRegex(CommandError, 'Unexpected clobber'):
            _sync_changelist(12345, 'test', '//myclient', '/ws',
                             expected_clobber=set())
        self.assertIn('\n/ws/a.txt\n', out.getvalue())


class TestBuildSyncTargets(unittest.TestCase):
    """Each of the user's changelists gets its own commit, in a strictly increasing sequence."""

    def test_single_own_changelist(self):
        changes = make_changes((100, 'other'), (101, 'other'),
                               (102, 'me'), (103, 'other'))
        targets = build_sync_targets(changes, ['me'], 100, [103])
        self.assertEqual(targets, [101, 102, 103])

    def test_user_names_match_case_insensitively(self):
        changes = make_changes((100, 'other'), (101, 'other'),
                               (102, 'Me'), (103, 'other'))
        targets = build_sync_targets(changes, ['me'], 100, [103])
        self.assertEqual(targets, [101, 102, 103])

    def test_two_own_changelists(self):
        changes = make_changes((100, 'other'), (101, 'other'), (102, 'me'),
                               (103, 'other'), (104, 'other'), (105, 'me'),
                               (106, 'other'))
        targets = build_sync_targets(changes, ['me'], 100, [106])
        self.assertEqual(targets, [101, 102, 104, 105, 106])

    def test_back_to_back_own_changelists(self):
        """A predecessor that is itself one of the user's changelists is not repeated."""
        changes = make_changes((100, 'other'), (101, 'other'),
                               (102, 'me'), (103, 'me'))
        targets = build_sync_targets(changes, ['me'], 100, [103])
        self.assertEqual(targets, [101, 102, 103])

    def test_predecessor_is_last_synced(self):
        """When the predecessor is last synced, only the user's changelist is added."""
        changes = make_changes((100, 'other'), (101, 'me'), (102, 'other'))
        targets = build_sync_targets(changes, ['me'], 100, [102])
        self.assertEqual(targets, [101, 102])

    def test_own_changelist_is_the_upper_bound(self):
        changes = make_changes((100, 'other'), (101, 'other'), (102, 'me'))
        targets = build_sync_targets(changes, ['me'], 100, [102])
        self.assertEqual(targets, [101, 102])

    def test_no_own_changelists(self):
        changes = make_changes((100, 'other'), (101, 'other'), (102, 'other'))
        targets = build_sync_targets(changes, ['me'], 100, [102])
        self.assertEqual(targets, [102])

    def test_no_changes_at_all(self):
        """With no changes in the range the upper bound is still synced."""
        targets = build_sync_targets([], ['me'], 100, [120])
        self.assertEqual(targets, [120])

    def test_user_match_is_case_insensitive(self):
        changes = make_changes((100, 'other'), (101, 'other'), (102, 'Me'))
        targets = build_sync_targets(changes, ['me'], 100, [103])
        self.assertEqual(targets, [101, 102, 103])

    def test_two_users_both_split_out(self):
        changes = make_changes((100, 'other'), (101, 'other'), (102, 'alice'),
                               (103, 'other'), (104, 'other'), (105, 'bob'),
                               (106, 'other'))
        targets = build_sync_targets(changes, ['alice', 'bob'], 100, [106])
        self.assertEqual(targets, [101, 102, 104, 105, 106])

    def test_adjacent_changelists_from_two_selected_users(self):
        """alice's changelist, the predecessor of bob's, is not repeated."""
        changes = make_changes((100, 'other'), (101, 'other'),
                               (102, 'alice'), (103, 'bob'))
        targets = build_sync_targets(changes, ['alice', 'bob'], 100, [103])
        self.assertEqual(targets, [101, 102, 103])

    def test_unselected_user_is_not_split_out(self):
        changes = make_changes((100, 'other'), (101, 'other'), (102, 'alice'),
                               (103, 'other'), (104, 'bob'))
        targets = build_sync_targets(changes, ['alice'], 100, [104])
        self.assertEqual(targets, [101, 102, 104])

    def test_targets_are_strictly_increasing(self):
        changes = make_changes((100, 'me'), (101, 'me'), (102, 'other'),
                               (103, 'me'), (104, 'me'), (105, 'other'))
        targets = build_sync_targets(changes, ['me'], 100, [105])
        self.assertEqual(targets, sorted(set(targets)))
        self.assertEqual(targets, [101, 102, 103, 104, 105])

    def test_required_changelists_between_split_points(self):
        changes = make_changes((100, 'other'), (101, 'other'), (102, 'me'),
                               (103, 'other'), (104, 'other'), (105, 'me'),
                               (106, 'other'))
        targets = build_sync_targets(changes, ['me'], 100, [103, 106])
        self.assertEqual(targets, [101, 102, 103, 104, 105, 106])

    def test_required_changelists_matching_split_points(self):
        """A required changelist that is also a split point is synced once."""
        changes = make_changes((100, 'other'), (101, 'other'), (102, 'me'),
                               (103, 'other'), (104, 'other'), (105, 'me'),
                               (106, 'other'))
        targets = build_sync_targets(changes, ['me'], 100, [102, 104, 106])
        self.assertEqual(targets, [101, 102, 104, 105, 106])

    def test_required_changelist_outside_the_depot_root(self):
        """A required changelist outside the depot root is still synced."""
        changes = make_changes((100, 'other'), (101, 'other'), (102, 'me'),
                               (103, 'other'))
        targets = build_sync_targets(changes, ['me'], 100, [110])
        self.assertEqual(targets, [101, 102, 110])

    def test_required_changelists_without_own_changelists(self):
        changes = make_changes((100, 'other'), (101, 'other'),
                               (102, 'other'), (103, 'other'))
        targets = build_sync_targets(changes, ['me'], 100, [101, 103])
        self.assertEqual(targets, [101, 103])


class TestCollectSplitUsers(unittest.TestCase):
    def _collect(self, configured, split_user=None, no_split=False,
                 known=None):
        args = _sync_args(changelist=[], split_user=split_user,
                          no_split=no_split)
        with mock.patch('git_p4son.sync.get_split_users',
                        return_value=configured), \
                mock.patch('git_p4son.sync_split_users.get_existing_p4_users',
                           return_value=list((known or {}).values())):
            return _collect_split_users(args, '/ws')

    def test_configured_users(self):
        self.assertEqual(self._collect(['alice']), ['alice'])

    def test_extra_users_follow_the_configured_ones(self):
        self.assertEqual(
            self._collect(['alice'], split_user=['BOB'], known={'BOB': 'bob'}),
            ['alice', 'bob'])

    def test_no_split_keeps_only_the_extra_users(self):
        self.assertEqual(
            self._collect(['alice'], split_user=['bob'], no_split=True,
                          known={'bob': 'bob'}),
            ['bob'])

    def test_unknown_extra_user_raises(self):
        with self.assertRaisesRegex(CommandError, 'No such Perforce user'):
            self._collect(['alice'], split_user=['nobody'])


class TestShouldSplit(unittest.TestCase):
    _last_synced = 100

    def test_splits_forward_from_the_last_sync(self):
        self.assertTrue(_should_split(['me'], self._last_synced,
                                      [(106, 'latest')]))

    def test_no_split_users(self):
        self.assertFalse(_should_split(
            [], self._last_synced, [(106, 'latest')]))

    def test_no_targets(self):
        self.assertFalse(_should_split(['me'], self._last_synced, []))

    def test_no_previous_sync(self):
        self.assertFalse(_should_split(['me'], None, [(106, 'latest')]))

    def test_syncing_backwards(self):
        self.assertFalse(_should_split(
            ['me'], self._last_synced, [(90, '90')]))


class SyncTestCase(P4RepoTestCase):
    """sync against a real git repository and a scripted Perforce depot."""

    def build_workspace(self):
        super().build_workspace()
        self.write_config('[depot]\nroot = "//myclient"\n')

    def sync(self, *argv):
        """Run sync with argv; returns everything it printed."""
        return self.run_command(sync_command, ['sync', *argv])

    def sync_fails(self, *argv, message):
        """Run sync with argv, expecting a CommandError matching message."""
        self.command_fails(sync_command, ['sync', *argv], message)

    def synced_commits(self):
        """The changelists of the sync commits after the setup's, oldest first."""
        subjects = self.git('log', '--reverse', '--format=%s').splitlines()
        return [int(s.rsplit('@', 1)[1]) for s in subjects[1:]
                if s.startswith('git-p4son: p4 sync')]

    def hook(self, name, body):
        """Add a Python hook script under .git-p4son/hooks/<name>/."""
        path = self.write_file(os.path.join('.git-p4son', 'hooks', name, 'hook.py'),
                               f'#!{sys.executable}\n{body}')
        os.chmod(path, stat.S_IRWXU)

    def hook_log(self):
        """A file outside the workspace for hooks to append to, and a reader for it."""
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        path = os.path.join(tempdir.name, 'hooks.log')

        def read():
            if not os.path.exists(path):
                return []
            with open(path) as f:
                return f.read().split()
        return path, read


class TestSync(SyncTestCase):
    def test_syncs_to_the_latest_changelist_by_default(self):
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.p4.submit(102, {'c.txt': 'c1\n'})
        self.sync()
        self.assertEqual(self.synced_commits(), [102])
        self.assertEqual(self.read('a.txt'), 'a2\n')
        self.assertEqual(self.read('c.txt'), 'c1\n')

    def test_explicit_head_keyword(self):
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.sync('head')
        self.assertEqual(self.synced_commits(), [101])

    def test_syncs_a_specific_changelist(self):
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.p4.submit(102, {'a.txt': 'a3\n'})
        self.sync('101')
        self.assertEqual(self.synced_commits(), [101])
        self.assertEqual(self.read('a.txt'), 'a2\n')

    def test_several_changelists_get_a_commit_each(self):
        for change, content in ((101, 'a2\n'), (102, 'a3\n'), (103, 'a4\n')):
            self.p4.submit(change, {'a.txt': content})
        self.sync('101', '103')
        self.assertEqual(self.synced_commits(), [101, 103])
        self.assertEqual(self.git('show', 'HEAD~1:a.txt'), 'a2')

    def test_trailing_head_closes_a_sequence(self):
        for change in (101, 102, 103):
            self.p4.submit(change, {'a.txt': f'a{change}\n'})
        self.sync('101', 'head')
        self.assertEqual(self.synced_commits(), [101, 103])

    def test_the_last_synced_changelist_in_a_list_is_skipped(self):
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.sync('100', '101')
        self.assertEqual(self.synced_commits(), [101])

    def test_already_synced_does_nothing(self):
        path, read_log = self.hook_log()
        self.hook('post-sync', f'open({path!r}, "a").write("post ")\n')
        out = self.sync('100')
        self.assertIn('Already at CL 100, nothing to sync', out)
        self.assertEqual(self.synced_commits(), [])
        self.assertEqual(self.p4.synced(), [])
        self.assertEqual(read_log(), [])

    def test_older_changelist_needs_force(self):
        self.p4.submit(50, {'a.txt': 'old\n'})
        self.sync_fails('50', message='without --force')
        self.sync('--force', '50')
        self.assertEqual(self.synced_commits(), [50])
        self.assertEqual(self.read('a.txt'), 'old\n')

    def test_force_sequence_may_start_before_the_last_sync(self):
        self.p4.submit(50, {'a.txt': 'old\n'})
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.sync('--force', '50', '101')
        self.assertEqual(self.synced_commits(), [50, 101])

    def test_invalid_arguments_fail_before_any_work(self):
        self.p4.submit(101, {'a.txt': 'a2\n'})
        for argv, message in (
                (['101', '101'], 'strictly increasing'),
                (['102', '101'], 'strictly increasing'),
                (['head', '101'], 'must come last'),
                (['last-synced', '101'], 'cannot be combined'),
                (['50', '101'], 'without --force'),
                (['12a'], 'Invalid changelist number')):
            with self.subTest(argv=argv):
                self.sync_fails(*argv, message=message)
        self.assertEqual(self.p4.synced(), [])
        self.assertEqual(self.synced_commits(), [])

    def test_workspace_placeholder_is_expanded(self):
        self.write_config('[depot]\nroot = "//$(workspace)"\n')
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.sync()
        self.assertEqual(self.git('log', '-1', '--format=%s'),
                         'git-p4son: p4 sync //myclient/...@101')

    def test_workspace_placeholder_needs_a_perforce_workspace(self):
        self.write_config('[depot]\nroot = "//$(workspace)"\n')
        self.p4.in_workspace = False
        self.sync_fails(message='Cannot resolve')

    def test_no_depot_root_fails(self):
        self.write_config('')
        self.sync_fails(message='No depot root configured')

    def test_dirty_git_workspace_fails_before_syncing(self):
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.write_file('stray.txt', 'x')
        self.sync_fails(message='not clean')
        self.assertEqual(self.p4.synced(), [])

    def test_tracked_files_opened_in_perforce_fail_the_sync(self):
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.p4.opened = {'a.txt': ('default', 'edit')}
        self.sync_fails(message='p4-opened files tracked by git')
        self.assertEqual(self.p4.synced(), [])

    def test_untracked_files_opened_in_perforce_are_allowed(self):
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.p4.opened = {'Content/level.umap': ('default', 'edit')}
        self.sync()
        self.assertEqual(self.synced_commits(), [101])


class TestSyncLocalChanges(SyncTestCase):
    def test_local_changes_are_merged_back_after_pure_perforce_commits(self):
        self.edit('a.txt', 'local\na1\n')
        self.commit('local change')
        self.p4.submit(101, {'a.txt': 'a1\nupstream\n'})
        out = self.sync()
        self.assertEqual(self.read('a.txt'), 'local\na1\nupstream\n')
        self.assertEqual(self.git('show', 'HEAD:a.txt'), 'a1\nupstream')
        self.assertIn('1 file merged successfully', out)

    def test_last_synced_resyncs_without_committing(self):
        self.p4.have = 0
        self.sync('last-synced')
        self.assertEqual(self.p4.synced(), [100])
        self.assertEqual(self.read('a.txt'), 'a1\n')
        self.assertEqual(self.synced_commits(), [])

    def test_last_synced_with_nothing_to_resync(self):
        self.sync('last-synced')
        self.assertEqual(self.p4.synced(), [])
        self.assertEqual(self.synced_commits(), [])

    def test_last_synced_without_a_previous_sync_fails(self):
        self.git('commit', '--amend', '-q', '-m', 'Initial')
        self.sync_fails('last-synced', message='No previous sync found')

    def test_ignored_writable_files_are_reported_not_synced(self):
        self.write_file('.gitignore', '*.log\n')
        self.p4.submit(101, {'x.log': 'one\n'})
        self.p4.sync_to(101)
        self.commit(format_sync_subject('//myclient', 101))
        self.edit('x.log', 'mine\n')
        self.p4.submit(102, {'x.log': 'two\n'})
        out = self.sync()
        self.assertEqual(self.read('x.log'), 'mine\n')
        self.assertIn('Files not synced (git-ignored and writable)', out)
        self.assertIn('x.log', out)

    def test_writable_mode_makes_synced_files_writable(self):
        self.write_config(
            '[depot]\nroot = "//myclient"\n[core]\nwritable = true\n')
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.sync()
        self.assertTrue(os.stat(os.path.join(self.ws, 'a.txt')).st_mode
                        & stat.S_IWUSR)


class TestSyncHooks(SyncTestCase):
    def test_pre_sync_hooks_run_once_for_a_sequence(self):
        path, read_log = self.hook_log()
        self.hook('pre-sync', f'open({path!r}, "a").write("pre ")\n')
        self.hook('post-sync', f'open({path!r}, "a").write("post ")\n')
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.p4.submit(102, {'a.txt': 'a3\n'})
        self.sync('101', '102')
        self.assertEqual(read_log(), ['pre', 'post'])

    def test_failing_pre_sync_hook_aborts_before_syncing(self):
        self.hook('pre-sync', 'raise SystemExit(1)\n')
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.sync_fails(message='pre-sync hook failed')
        self.assertEqual(self.p4.synced(), [])

    @unittest.skipUnless(sys.platform == 'win32', 'PowerShell hooks run through the Windows .ps1 association')
    def test_powershell_hook_runs(self):
        path, read_log = self.hook_log()
        # PowerShell escapes a quote inside single quotes by doubling it.
        quoted = path.replace("'", "''")
        self.write_file(os.path.join('.git-p4son', 'hooks', 'post-sync', 'hook.ps1'),
                        f"Add-Content -Path '{quoted}' -Value 'ps1'\n")
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.sync('101')
        self.assertEqual(read_log(), ['ps1'])


class TestSyncDryRun(SyncTestCase):
    def test_prints_the_sequence_and_syncs_nothing(self):
        for change in (101, 102):
            self.p4.submit(change, {'a.txt': f'a{change}\n'})
        self.write_file('stray.txt', 'dry runs skip the checks')
        out = self.sync('-n', '101', 'head')
        self.assertIn('Sync sequence', out)
        self.assertIn('101 102', out)
        self.assertEqual(self.p4.synced(), [])
        self.assertEqual(self.synced_commits(), [])

    def test_last_synced(self):
        out = self.sync('-n', 'last-synced')
        self.assertIn('100 (last synced)', out)

    def test_invalid_arguments_still_fail(self):
        self.sync_fails('-n', '50', message='without --force')


class TestSyncSplitting(SyncTestCase):
    """Last synced at 100, latest 106; me submitted 102 and 105."""

    def setUp(self):
        super().setUp()
        for change in range(101, 107):
            self.p4.submit(change, {'a.txt': f'a{change}\n'},
                           user='me' if change in (102, 105) else 'other')

    def configure(self, *users):
        listed = ', '.join(f'"{u}"' for u in users)
        self.write_config(
            f'[depot]\nroot = "//myclient"\n[sync]\nsplit-users = [{listed}]\n')

    def test_configured_users_get_commits_of_their_own(self):
        self.configure('me')
        self.sync()
        self.assertEqual(self.synced_commits(), [101, 102, 104, 105, 106])

    def test_placeholder_is_the_current_user(self):
        self.configure('$(user)')
        self.sync()
        self.assertEqual(self.synced_commits(), [101, 102, 104, 105, 106])

    def test_unresolvable_placeholder_fails_without_syncing(self):
        self.configure('$(user)')
        self.p4.user = '*unknown*'
        self.sync_fails(message='--no-split')
        self.assertEqual(self.p4.synced(), [])

    def test_explicit_target_bounds_the_range(self):
        self.configure('me')
        self.sync('103')
        self.assertEqual(self.synced_commits(), [101, 102, 103])

    def test_split_user_option_adds_a_user(self):
        self.sync('-u', 'me')
        self.assertEqual(self.synced_commits(), [101, 102, 104, 105, 106])

    def test_unknown_split_user_fails_before_any_work(self):
        self.sync_fails(
            '-u', 'nobody', message='No such Perforce user: nobody')
        self.assertEqual(self.p4.synced(), [])

    def test_no_split_ignores_the_configured_users(self):
        self.configure('me')
        self.sync('--no-split')
        self.assertEqual(self.synced_commits(), [106])

    def test_first_sync_is_not_split(self):
        self.configure('me')
        self.git('commit', '--amend', '-q', '-m', 'Initial')
        out = self.sync()
        self.assertIn('no previous sync to start from', out)
        self.assertEqual(self.p4.synced(), [106])

    def test_dry_run_prints_the_split_sequence(self):
        self.configure('me')
        out = self.sync('-n')
        self.assertIn('101 102 104 105 106', out)
        self.assertEqual(self.p4.synced(), [])


class TestSyncPreflight(unittest.TestCase):
    """The preflight runs blocking processes, workspace checks, then hooks, in that order."""

    @mock.patch('git_p4son.sync.run_hooks')
    @mock.patch('git_p4son.sync.p4_get_opened_files')
    @mock.patch('git_p4son.lib.get_dirty_files')
    @mock.patch('git_p4son.sync.check_no_blocking_processes',
                side_effect=CommandError('blocked'))
    def test_blocking_process_stops_before_everything_else(
            self, mock_blocking, mock_dirty, mock_opened, mock_run_hooks):
        with self.assertRaisesRegex(CommandError, 'blocked'):
            sync_preflight(
                '//ws', '/ws', '/invoked', ignore_blocking_processes=False)
        mock_blocking.assert_called_once_with('/ws', False)
        mock_dirty.assert_not_called()
        mock_opened.assert_not_called()
        mock_run_hooks.assert_not_called()

    @mock.patch('git_p4son.sync.run_hooks', return_value=[])
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.lib.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.check_no_blocking_processes')
    def test_ignore_blocking_processes_is_passed_on(
            self, mock_blocking, _dirty, _opened, _hooks):
        sync_preflight('//ws', '/ws', '/invoked',
                       ignore_blocking_processes=True)
        mock_blocking.assert_called_once_with('/ws', True)

    @mock.patch('git_p4son.sync.run_hooks', return_value=[])
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.lib.get_dirty_files', return_value=[])
    def test_clean_workspaces_run_the_hooks(
            self, _dirty, _opened, mock_run_hooks):
        sync_preflight(
            '//ws', '/ws', '/invoked', ignore_blocking_processes=False)
        mock_run_hooks.assert_called_once_with('pre-sync', '/ws', '/invoked')

    @mock.patch('git_p4son.sync.run_hooks')
    @mock.patch('git_p4son.sync.p4_get_opened_files')
    @mock.patch('git_p4son.lib.get_dirty_files',
                return_value=[('a.txt', 'modify')])
    def test_dirty_git_stops_before_p4_and_hooks(
            self, _dirty, mock_opened, mock_run_hooks):
        with self.assertRaisesRegex(CommandError, 'not clean'):
            sync_preflight(
                '//ws', '/ws', '/invoked', ignore_blocking_processes=False)
        mock_opened.assert_not_called()
        mock_run_hooks.assert_not_called()

    @mock.patch('git_p4son.sync.run_hooks')
    @mock.patch('git_p4son.sync.get_tracked_files', return_value={'a.txt'})
    @mock.patch('git_p4son.sync.p4_get_opened_files',
                return_value=[('a.txt', 'modify')])
    @mock.patch('git_p4son.lib.get_dirty_files', return_value=[])
    def test_dirty_p4_stops_before_hooks(
            self, _dirty, _opened, _tracked, mock_run_hooks):
        with self.assertRaisesRegex(CommandError, 'p4-opened files tracked by git'):
            sync_preflight(
                '//ws', '/ws', '/invoked', ignore_blocking_processes=False)
        mock_run_hooks.assert_not_called()

    @mock.patch('git_p4son.sync.run_hooks',
                return_value=[make_run_result(returncode=1)])
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.lib.get_dirty_files', return_value=[])
    def test_failing_hook_fails_the_gate(self, _dirty, _opened, _hooks):
        with self.assertRaisesRegex(CommandError, 'pre-sync hook failed'):
            sync_preflight(
                '//ws', '/ws', '/invoked', ignore_blocking_processes=False)


if __name__ == '__main__':
    unittest.main()
