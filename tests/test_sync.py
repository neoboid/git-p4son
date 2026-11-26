"""Tests for git_p4son.sync module."""

import datetime
import os
import stat
import sys
import tempfile
import unittest
from unittest import mock

from git_p4son import __version__
from git_p4son.common import CommandError, RunError, join_command_line
from git_p4son.git import format_sync_subject, merge_file
from git_p4son.git_backend import GitBackend
from git_p4son.perforce import P4SyncAbortError, P4SyncClobberError
from git_p4son.cherry_pick import cherry_pick_command
from git_p4son.sync import (
    build_sync_targets,
    _collect_split_users,
    _make_synced_files_writable,
    _should_split,
    SyncPass,
    _sync_changelist,
    sync_command,
    sync_preflight,
)
from tests.helpers import (
    on_jj,
    captured_output,
    cli_args,
    make_run_result,
    P4RepoTestCase,
    save_config,
    start_patch,
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


@mock.patch('git_p4son.sync.p4_sync', return_value=[])
class TestSyncChangelist(unittest.TestCase):
    """p4 refusing an expected (git-ignored) file is tolerated; any other refusal raises."""

    def _clobber(self, *refused):
        return P4SyncClobberError(
            RunError('p4 sync failed', 1, []), list(refused))

    def test_clean_sync_refuses_nothing(self, mock_p4_sync):
        with captured_output() as out:
            _sync_changelist(SyncPass(12345, 'test'),
                             '//myclient', '/ws', expected_refusals=set())
        mock_p4_sync.assert_called_once_with('@12345', '//myclient', '/ws')
        self.assertNotIn('refusal', out.getvalue())

    def test_expected_clobber_tolerated(self, mock_p4_sync):
        mock_p4_sync.side_effect = self._clobber('/ws/a.txt', '/ws/b.txt')
        with captured_output() as out:
            _sync_changelist(SyncPass(12345, 'test'), '//myclient', '/ws',
                             expected_refusals={'/ws/a.txt', '/ws/b.txt'})
        self.assertIn('2 expected refusals', out.getvalue())

    def test_failed_rename_aborts_with_advice(self, mock_p4_sync):
        mock_p4_sync.side_effect = P4SyncAbortError(
            'rename: failed to rename D:\\ws\\a.uasset')
        with captured_output() as out, \
                self.assertRaises(CommandError) as ctx:
            _sync_changelist(SyncPass(12345, 'test'), '//myclient',
                             '/ws', expected_refusals=set())
        self.assertIn('\nrename: failed to rename D:\\ws\\a.uasset\n',
                      out.getvalue())
        self.assertIn('partially synced', str(ctx.exception))

    def test_unexpected_clobber_raises(self, mock_p4_sync):
        mock_p4_sync.side_effect = self._clobber('/ws/a.txt')
        with captured_output() as out, \
                self.assertRaisesRegex(CommandError, 'unexpectedly refused'):
            _sync_changelist(SyncPass(12345, 'test'), '//myclient', '/ws',
                             expected_refusals=set())
        self.assertIn('\n/ws/a.txt\n', out.getvalue())


class TestBuildSyncTargets(unittest.TestCase):
    """Each split changelist, after the workspace changelist before it, in a strictly increasing sequence."""

    def test_split_changelists_follow_their_predecessors(self):
        self.assertEqual(build_sync_targets(
            [(102, 101), (105, 104)], 100, [106]), [101, 102, 104, 105, 106])

    def test_predecessor_already_synced_or_missing(self):
        """When the predecessor is last synced, older or missing, only the split changelist is added."""
        self.assertEqual(build_sync_targets(
            [(101, 100), (103, 90), (105, 0)], 100, [106]), [101, 103, 105, 106])

    def test_changelists_appear_once(self):
        """A predecessor that is split out or required too is synced once."""
        self.assertEqual(build_sync_targets(
            [(102, 101), (103, 102)], 100, [101, 103]), [101, 102, 103])

    def test_no_split_changelists(self):
        self.assertEqual(build_sync_targets([], 100, [120]), [120])


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
        """Run sync with argv, expecting a CommandError matching message; returns what it printed."""
        return self.command_fails(sync_command, ['sync', *argv], message)

    def synced_commits(self):
        """The changelists of the sync commits after the setup's, oldest first."""
        subjects = self.git('log', '--reverse', '--format=%s').splitlines()
        return [int(s.rsplit('@', 1)[1]) for s in subjects[1:]
                if s.startswith('git-p4son: p4 sync')]

    def commit_local_changes(self, *paths):
        """Commit a local change to each of paths on the sync branch; the base commit they are measured from."""
        for path in paths:
            self.edit(path, f'local\n{self.read(path)}')
        self.commit('Local changes')
        return self.git('rev-parse', 'HEAD~1')

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

    def test_older_changelist_needs_allow_backwards(self):
        self.p4.submit(50, {'a.txt': 'old\n'})
        self.sync_fails('50', message='without --allow-backwards')
        out = self.sync('--allow-backwards', '50')
        self.assertIn(
            '[warn] Syncing to older CL 50 (currently at CL 100)\n', out)
        self.assertEqual(self.synced_commits(), [50])
        self.assertEqual(self.read('a.txt'), 'old\n')

    def test_deprecated_force_allows_backwards_with_a_warning(self):
        self.p4.submit(50, {'a.txt': 'old\n'})
        out = self.sync('-n', '-f', '50')
        self.assertIn(
            '--force is deprecated, use --allow-backwards instead', out)
        self.assertIn('Syncing to older CL 50', out)

    def test_sequence_may_start_before_the_last_sync(self):
        self.p4.submit(50, {'a.txt': 'old\n'})
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.sync('--allow-backwards', '50', '101')
        self.assertEqual(self.synced_commits(), [50, 101])

    def test_invalid_arguments_fail_before_any_work(self):
        self.p4.submit(101, {'a.txt': 'a2\n'})
        for argv, message in (
                (['101', '101'], 'strictly increasing'),
                (['102', '101'], 'strictly increasing'),
                (['head', '101'], 'must come last'),
                (['last-synced', '101'], 'cannot be combined'),
                (['50', '101'], 'without --allow-backwards'),
                (['12a'], 'neither a changelist number nor a sync target')):
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
        out = self.sync()
        self.assertEqual(self.synced_commits(), [101])
        self.assertIn('~ Content/level.umap', out)


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
        self.assertEqual(self.p4.synced(), ['@100'])
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

    def test_a_git_ignored_add_of_a_plus_w_type_over_a_writable_file_is_reported_not_synced(self):
        self.write_file('.gitignore', '*.log\n')
        self.commit('Ignore logs')
        self.write_file('x.log', 'mine\n')
        self.p4.always_writable.add('x.log')
        self.p4.submit(101, {'a0.txt': 'a\n', 'x.log': 'p4\n'})
        out = self.sync('101')
        self.assertEqual(self.read('x.log'), 'mine\n')
        self.assertIn('Files not synced (git-ignored and writable)', out)
        self.assertIn('x.log', out.split(
            'Files not synced (git-ignored and writable)', 1)[1])
        self.assertEqual(self.synced_commits(), [101])

    def test_a_local_change_to_a_file_with_p4_wildcards_in_its_name_is_merged_back(self):
        self.commit_sync(101, {'d/at@sign#1%.txt': 'x\n'})
        self.edit('d/at@sign#1%.txt', 'local\nx\n')
        self.commit('local change')
        self.p4.submit(102, {'d/at@sign#1%.txt': 'x\nupstream\n'})
        out = self.sync()
        self.assertEqual(self.read('d/at@sign#1%.txt'), 'local\nx\nupstream\n')
        self.assertIn('1 file merged successfully', out)

    def test_a_read_only_local_change_is_merged_back(self):
        """As after git p4son writable off, which makes committed tweaks read-only too."""
        self.edit('a.txt', 'local\na1\n')
        self.commit('local change')
        os.chmod(os.path.join(self.ws, 'a.txt'), stat.S_IRUSR)
        self.p4.submit(101, {'a.txt': 'a1\nupstream\n'})
        out = self.sync()
        self.assertEqual(self.git('show', 'HEAD:a.txt'), 'a1\nupstream')
        self.assertEqual(self.read('a.txt'), 'local\na1\nupstream\n')
        self.assertIn('1 file merged successfully', out)

    def test_a_file_deleted_in_a_local_commit_is_synced_without_a_report(self):
        self.git('rm', '-q', 'a.txt')
        self.commit('drop a')
        self.p4.submit(101, {'a.txt': 'a1\nupstream\n'})
        out = self.sync()
        self.assertEqual(self.read('a.txt'), 'a1\nupstream\n')
        self.assertNotIn('not synced', out)
        self.assertNotIn('will not be synced', out)

    def test_an_allwrite_workspace_is_refused_with_nothing_changed(self):
        self.p4.submit(101, {'a.txt': 'a1\nupstream\n'})
        head = self.git('rev-parse', 'HEAD')
        for options in ('allwrite noclobber', 'allwrite clobber'):
            with self.subTest(options):
                self.p4.options = options
                self.sync_fails('101', message='allwrite option, which git-p4son does not support.*noallwrite.*'
                                '"git p4son writable enable"')
                self.assertEqual(self.git('rev-parse', 'HEAD'), head)
                self.assertEqual(self.p4.have, 100)

    def test_a_plus_w_file_an_earlier_pass_added_is_no_local_change(self):
        self.p4.always_writable.add('w.txt')
        self.p4.submit(101, {'w.txt': 'w1\n'})
        self.p4.submit(102, {'w.txt': 'w2\n'})
        out = self.sync('101', '102')
        self.assertNotIn('has local changes', out)
        self.assertNotIn('deleted locally', out)
        self.assertEqual(self.read('w.txt'), 'w2\n')
        self.assertEqual(self.synced_commits(), [101, 102])

    def test_writable_mode_makes_synced_files_writable(self):
        self.write_config(
            '[depot]\nroot = "//myclient"\n[core]\nwritable = true\n')
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.sync()
        self.assertTrue(os.stat(os.path.join(self.ws, 'a.txt')).st_mode
                        & stat.S_IWUSR)


class TestSyncProgress(SyncTestCase):
    """sync keeps what it has left to do in the sync progress, from its first p4 sync until its end has run."""

    def test_an_interruption_in_the_first_pass_keeps_its_lists_and_the_passes_left(self):
        base = self.commit_local_changes('a.txt')
        pre_sync = self.git('rev-parse', 'HEAD')
        self.p4.submit(101, {'a.txt': 'a1\nupstream\n', 'd.txt': 'd1\n'})
        self.p4.submit(102, {'e.txt': 'e1\n'})
        self.p4.interrupt_sync('@101', after=1)
        progress = self.interrupted_sync('101', '102')
        self.assertEqual({key: progress[key] for key in (
            'version', 'written-by', 'sync-branch', 'depot-root', 'tip', 'pre-sync-commit')}, {
            'version': 1, 'written-by': __version__, 'sync-branch': 'main', 'depot-root': '//myclient',
            'tip': pre_sync, 'pre-sync-commit': pre_sync})
        self.assertIsInstance(progress['started'], datetime.datetime)
        self.assertEqual((progress['start']['backend'], progress['start']['on-sync-branch']),
                         (self.backend_name or 'git', True))
        self.assertEqual(progress['passes'], [
            {'changelist': 101, 'label': 'specified', 'subject': 'git-p4son: p4 sync //myclient/...@101',
             'files': [['upd', 'a.txt'], ['add', 'd.txt']], 'changed': [{'path': 'a.txt', 'base': base}],
             'ignored': [], 'always-writable': []},
            {'changelist': 102, 'label': 'specified', 'subject': 'git-p4son: p4 sync //myclient/...@102'}])
        self.assertEqual(progress['end'], {
                         'changed': [], 'merged': [], 'ignored': [], 'synced': []})

    def test_an_interruption_in_a_later_pass_keeps_the_commit_made_and_its_local_changes(self):
        base = self.commit_local_changes('a.txt')
        self.p4.submit(101, {'a.txt': 'a1\nupstream\n'})
        self.p4.submit(102, {'d.txt': 'd1\n', 'e.txt': 'e1\n'})
        self.p4.interrupt_sync('@102', after=1)
        progress = self.interrupted_sync('101', '102')
        self.assertEqual(self.git('log', '-1', '--format=%s'),
                         'git-p4son: p4 sync //myclient/...@101')
        self.assertEqual(progress['tip'], self.git('rev-parse', 'HEAD'))
        self.assertEqual([p['changelist'] for p in progress['passes']], [102])
        self.assertEqual(progress['passes'][0]['files'], [
                         ['add', 'd.txt'], ['add', 'e.txt']])
        self.assertEqual(progress['end']['changed'], [
                         {'path': 'a.txt', 'base': base}])

    def test_an_interruption_in_the_merge_back_keeps_the_files_left_and_the_outcomes_so_far(self):
        base = self.commit_local_changes('a.txt', 'b.txt')
        self.p4.submit(101, {'a.txt': 'upstream\na1\n',
                       'b.txt': 'b1\nupstream\n'})
        merged: list[str] = []

        def merge_one_then_interrupt(*args):
            if merged:
                raise KeyboardInterrupt
            merged.append(args[0])
            return merge_file(*args)
        start_patch(self, 'git_p4son.sync_merge.merge_file',
                    side_effect=merge_one_then_interrupt)
        progress = self.interrupted_sync()
        self.assertIn('<<<<<<< Perforce', self.read('a.txt'))
        self.assertEqual(progress['tip'], self.git('rev-parse', 'HEAD'))
        self.assertEqual(progress['passes'], [])
        self.assertEqual(progress['end']['changed'], [
                         {'path': 'b.txt', 'base': base}])
        self.assertEqual(progress['end']['merged'], [
                         {'path': 'a.txt', 'outcome': 'conflicts'}])

    def test_the_ignored_files_of_the_passes_made_are_kept_for_the_report(self):
        self.write_file('.gitignore', '*.log\n')
        self.commit_sync(101, {'x.log': 'one\n'})
        self.edit('x.log', 'mine\n')
        self.p4.submit(102, {'x.log': 'two\n'})
        self.p4.submit(103, {'d.txt': 'd1\n'})
        self.p4.interrupt_sync('@103', after=1)
        progress = self.interrupted_sync('102', '103')
        self.assertEqual(progress['end']['ignored'], ['x.log'])

    def test_in_writable_mode_the_files_synced_are_kept_for_the_end(self):
        self.write_config(
            '[depot]\nroot = "//myclient"\n[core]\nwritable = true\n')
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.p4.submit(102, {'d.txt': 'd1\n'})
        self.p4.interrupt_sync('@102', after=1)
        progress = self.interrupted_sync('101', '102')
        self.assertEqual(progress['end']['synced'], ['a.txt'])

    def test_a_finished_sync_removes_it_before_the_post_sync_hooks(self):
        path, read_log = self.hook_log()
        progress = os.path.join(self.ws, '.git-p4son', 'sync-progress.toml')
        code = (f'import os, sys\nopen({path!r}, "a").write('
                f'sys.argv[1] + " " + str(os.path.exists({progress!r})) + " ")\n')
        self.hook('pre-sync', code, 'pre')
        self.hook('post-sync', code, 'post')
        self.commit_local_changes('a.txt')
        self.p4.submit(101, {'a.txt': 'a1\nupstream\n'})
        self.sync()
        self.assertEqual(read_log(), ['pre', 'False', 'post', 'False'])
        self.assertIsNone(self.sync_progress_file())

    def test_a_dry_run_writes_none(self):
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.sync('--dry-run')
        self.assertIsNone(self.sync_progress_file())


class TestSyncResume(SyncTestCase):
    """A plain sync finishes an interrupted sync from its sync progress."""

    def build_workspace(self):
        super().build_workspace()
        self.base = self.commit_local_changes('a.txt')
        self.p4.submit(101, {'a.txt': 'a1\nupstream\n',
                       'd.txt': 'd1\n', 'e.txt': 'e1\n'})
        self.p4.submit(102, {'f.txt': 'f1\n', 'g.txt': 'g1\n'})

    def assert_synced_as_if_uninterrupted(self):
        """The end of an uninterrupted sync 101 102: a commit of each with Perforce's files, the local change merged
        back, and no sync progress left."""
        self.assertEqual(self.synced_commits(), [101, 102])
        for commit, change in (('main~1', 101), ('main', 102)):
            for path, content in self.p4.files_at(change).items():
                self.assertEqual(
                    self.git('show', f'{commit}:{path}'), content.rstrip('\n'), (commit, path))
        self.assertEqual({name: self.read(name) for name in ('a.txt', 'd.txt', 'e.txt', 'f.txt', 'g.txt')},
                         {'a.txt': 'local\na1\nupstream\n', 'd.txt': 'd1\n', 'e.txt': 'e1\n', 'f.txt': 'f1\n',
                          'g.txt': 'g1\n'})
        self.assertIsNone(self.sync_progress_file())

    def test_an_interruption_in_the_first_pass_resumes_with_only_its_files_left(self):
        self.p4.interrupt_sync('@101', after=2)
        self.interrupted_sync('101', '102')
        self.p4.transferred.clear()
        out = self.sync()
        self.assertIn(
            'Resuming the interrupted sync: 2 changelists left, to CL 102', out)
        self.assertEqual(self.p4.transferred, ['e.txt', 'f.txt', 'g.txt'])
        self.assert_synced_as_if_uninterrupted()

    def test_an_interruption_in_a_later_pass_keeps_the_commit_made(self):
        self.p4.interrupt_sync('@102', after=1)
        self.interrupted_sync('101', '102')
        self.p4.transferred.clear()
        out = self.sync()
        self.assertIn(
            'Resuming the interrupted sync: 1 changelist left, to CL 102', out)
        self.assertEqual(self.p4.transferred, ['g.txt'])
        self.assert_synced_as_if_uninterrupted()

    def test_with_only_the_merge_back_left_it_merges_the_rest_once(self):
        self.commit_local_changes('b.txt')
        self.p4.submit(103, {'b.txt': 'b1\nupstream\n'})
        merged: list[str] = []

        def merge_one_then_interrupt(*args):
            if merged:
                raise KeyboardInterrupt
            merged.append(args[0])
            return merge_file(*args)
        with mock.patch('git_p4son.sync_merge.merge_file', side_effect=merge_one_then_interrupt):
            self.interrupted_sync('103')
        self.p4.transferred.clear()
        out = self.sync()
        self.assertIn(
            'Resuming the interrupted sync: only the merge-back is left', out)
        self.assertEqual(self.p4.transferred, [])
        self.assertEqual((self.read('a.txt'), self.read('b.txt')),
                         ('local\na1\nupstream\n', 'local\nb1\nupstream\n'))
        self.assertIn('2 files merged successfully', out)
        self.assertIsNone(self.sync_progress_file())

    def test_a_resume_interrupted_again_carries_on(self):
        self.p4.interrupt_sync('@101', after=1)
        self.interrupted_sync('101', '102')
        self.p4.interrupt_sync('@101', after=1)
        self.interrupted_sync()
        self.p4.transferred.clear()
        self.sync()
        self.assertEqual(self.p4.transferred, ['e.txt', 'f.txt', 'g.txt'])
        self.assert_synced_as_if_uninterrupted()

    def test_a_dry_run_says_what_is_left_and_changes_nothing(self):
        self.p4.interrupt_sync('@101', after=2)
        progress = self.interrupted_sync('101', '102')
        synced = list(self.p4.synced())
        out = self.sync('--dry-run')
        self.assertIn(
            'Resuming the interrupted sync: 2 changelists left, to CL 102', out)
        self.assertIn('2 files synced by the interrupted run', out)
        self.assertEqual(self.p4.synced(), synced)
        self.assertEqual(self.sync_progress_file(), progress)

    def test_a_failing_pre_sync_hook_leaves_the_interrupted_sync_as_it_was(self):
        self.p4.interrupt_sync('@101', after=2)
        progress = self.interrupted_sync('101', '102')
        head = self.git('rev-parse', 'HEAD')
        self.hook('pre-sync', 'raise SystemExit(1)\n')
        self.sync_fails(message='pre-sync hook failed')
        self.assertEqual(self.sync_progress_file(), progress)
        self.assertEqual(self.git('rev-parse', 'HEAD'), head)
        self.assertEqual(self.read('d.txt'), 'd1\n')

    def test_the_files_a_catch_up_pass_changed_are_resumed_with_the_pass_after_it(self):
        self.p4.submit(103, {'x.txt': 'x1\n'})
        self.sync('101')
        self.commit('Merged back')
        self.run_command(cherry_pick_command, ['cherry-pick', '103'])
        # sync 102 catches up to CL 101 alone first, which takes the cherry-pick of CL 103 back.
        self.p4.interrupt_sync('@102', after=1)
        progress = self.interrupted_sync('102')
        self.assertEqual(progress['uncommitted'], [['del', 'x.txt']])
        self.sync()
        self.assertEqual(self.git('log', '-1', '--format=%s',
                         'main'), 'git-p4son: p4 sync //myclient/...@102')
        self.assertNotIn('x.txt', self.git(
            'ls-tree', '--name-only', 'main').splitlines())
        self.assertFalse(os.path.exists(os.path.join(self.ws, 'x.txt')))
        self.assertIsNone(self.sync_progress_file())

    def test_a_file_the_pass_adds_that_p4_has_not_recorded_is_refused_for_now(self):
        self.p4.interrupt_sync('@101', after=1)
        self.interrupted_sync('101', '102')
        self.write_file('e.txt', 'partial\n')
        out = self.sync_fails(message='changed since the interrupted sync')
        self.assertIn('e.txt', out.splitlines())

    def test_a_file_changed_since_the_interruption_is_refused_for_now(self):
        self.p4.interrupt_sync('@101', after=2)
        progress = self.interrupted_sync('101', '102')
        self.write_file('new.txt', 'mine\n')
        os.chmod(os.path.join(self.ws, 'd.txt'), stat.S_IRUSR | stat.S_IWUSR)
        self.edit('d.txt', 'mine\n')
        out = self.sync_fails(message='changed since the interrupted sync')
        listed = out.splitlines()
        self.assertIn('new.txt', listed)
        self.assertIn('d.txt', listed)
        self.assertNotIn('a.txt', listed)
        self.assertEqual(self.sync_progress_file(), progress)

    def test_writable_mode_makes_every_synced_file_writable(self):
        self.write_config(
            '[depot]\nroot = "//myclient"\n[core]\nwritable = true\n')
        self.p4.interrupt_sync('@101', after=2)
        self.interrupted_sync('101', '102')
        self.sync()
        for name in ('a.txt', 'd.txt', 'e.txt', 'f.txt'):
            self.assertTrue(os.stat(os.path.join(self.ws, name)
                                    ).st_mode & stat.S_IWUSR, name)

    def test_a_workspace_switched_to_allwrite_while_interrupted_is_refused_with_the_progress_kept(self):
        self.p4.interrupt_sync('@101', after=1)
        progress = self.interrupted_sync('101', '102')
        self.p4.options = 'allwrite noclobber'
        self.sync_fails(
            message='allwrite option, which git-p4son does not support')
        self.assertEqual(self.sync_progress_file(), progress)


class TestSyncBranch(SyncTestCase):
    """sync commits onto the sync branch from any branch, and returns to where it started."""

    def branch(self):
        return self.git('branch', '--show-current')

    def config(self, extra):
        self.write_config(f'[depot]\nroot = "//myclient"\n\n{extra}')

    def interrupt(self, revision='@101'):
        """Submit CL 101 and 102, then have sync 101 102 interrupted in the sync to revision."""
        self.p4.submit(101, {'d.txt': 'd1\n', 'e.txt': 'e1\n'})
        self.p4.submit(102, {'f.txt': 'f1\n', 'g.txt': 'g1\n'})
        self.p4.interrupt_sync(revision, after=1)
        return self.interrupted_sync('101', '102')

    def test_an_interrupted_sync_says_to_run_sync_again_to_finish(self):
        self.commit_feature()
        self.p4.submit(101, {'d.txt': 'd1\n', 'e.txt': 'e1\n'})
        self.p4.interrupt_sync('@101', after=1)
        with captured_output() as out, self.assertRaises(KeyboardInterrupt), self.command_args(['sync']) as args:
            sync_command(args)
        self.assertIn('Run "git p4son sync" to finish it', out.getvalue())
        self.assertNotIn('once it is sorted out', out.getvalue())

    def test_a_resume_returns_to_where_the_sync_started(self):
        self.commit_feature()
        feature = self.git('rev-parse', 'HEAD')
        self.interrupt()
        out = self.sync()
        self.assert_at('feature', feature)
        self.assertEqual(self.git('log', '--format=%s', '-2', 'main').splitlines(),
                         ['git-p4son: p4 sync //myclient/...@102', 'git-p4son: p4 sync //myclient/...@101'])
        self.assertIn('main is now at CL 102', out)

    def test_a_resume_refuses_while_head_is_off_the_sync_branch(self):
        self.interrupt()
        self.git('switch', '-q', '-c', 'other')
        self.sync_fails(message='git switch main')

    def test_a_resume_refuses_a_sync_branch_without_the_recorded_tip(self):
        progress = self.interrupt(revision='@102')
        self.git('update-ref', 'refs/heads/main', 'main~1')
        self.sync_fails(message=f'git reset --keep {progress["tip"]}')

    def test_a_resume_refuses_commits_above_the_recorded_tip_for_now(self):
        self.interrupt()
        self.git('commit', '-q', '--allow-empty', '-m', 'Mine')
        out = self.sync_fails(
            message='made on main after the interrupted sync')
        self.assertIn('Mine', out)

    def test_a_resume_stays_on_the_sync_branch_when_the_start_branch_is_gone(self):
        self.commit_feature()
        self.interrupt()
        self.git('branch', '-q', '-D', 'feature')
        out = self.sync()
        self.assertEqual(self.branch(), 'main')
        self.assertIn('feature, where sync started, no longer exists', out)

    def test_on_the_sync_branch_it_stays_there(self):
        self.p4.submit(101, {'d.txt': 'd1\n'})
        out = self.sync()
        self.assertEqual(self.branch(), 'main')
        self.assertEqual(self.synced_commits(), [101])
        self.assertNotIn('Returning to', out)

    def test_from_another_branch_it_syncs_the_sync_branch_and_returns(self):
        self.commit_feature()
        feature = self.git('rev-parse', 'HEAD')
        self.p4.submit(101, {'d.txt': 'd1\n'})
        out = self.sync()
        self.assert_at('feature', feature)
        self.assertEqual(self.git('log', '-1', '--format=%s',
                         'main'), 'git-p4son: p4 sync //myclient/...@101')
        self.assertFalse(os.path.exists(os.path.join(self.ws, 'd.txt')))
        self.assertEqual(self.read('a.txt'), 'a2\n')
        self.assertIn('main is now at CL 101. Run "git rebase main"', out)

    def test_a_ctrl_c_in_the_post_sync_hooks_stops_them_and_sync_still_returns(self):
        self.commit_feature()
        feature = self.git('rev-parse', 'HEAD')
        self.p4.submit(101, {'d.txt': 'd1\n'})
        out = self.p4son_stopped_in_post_sync_hooks('sync')
        self.assert_at('feature', feature)
        self.assertEqual(self.git('log', '-1', '--format=%s',
                         'main'), 'git-p4son: p4 sync //myclient/...@101')
        self.assertIn('main is now at CL 101', out)

    def test_an_error_in_the_post_sync_hooks_stops_them_and_sync_still_returns(self):
        self.commit_feature()
        feature = self.git('rev-parse', 'HEAD')
        self.p4.submit(101, {'d.txt': 'd1\n'})
        self.p4son_stopped_in_post_sync_hooks(
            'sync', error=CommandError('No Perforce user for $(user)'),
            message='post-sync hooks stopped: No Perforce user for $(user)')
        self.assert_at('feature', feature)
        self.assertEqual(self.git('log', '-1', '--format=%s',
                         'main'), 'git-p4son: p4 sync //myclient/...@101')

    def test_a_failed_p4_command_in_the_post_sync_hooks_names_its_error(self):
        expired = RunError(
            join_command_line(['p4', 'user', '-o']), stderr=['Your session has expired, please login again.'])
        out = self.p4son_stopped_in_post_sync_hooks(
            'sync', 'last-synced', error=expired,
            message='post-sync hooks stopped: p4 user -o failed with return code 1')
        self.assertIn('Your session has expired, please login again.', out)

    def test_the_sync_progress_records_the_start_branch(self):
        self.commit_feature()
        self.p4.submit(101, {'d.txt': 'd1\n'})
        self.p4.interrupt_sync('@101', after=1)
        progress = self.interrupted_sync()
        self.assertEqual(progress['start'], {
                         'backend': 'git', 'branch': 'feature', 'on-sync-branch': False})

    def test_the_sync_progress_records_a_detached_start_by_its_commit(self):
        self.git('switch', '-q', '--detach')
        start = self.git('rev-parse', 'HEAD')
        self.p4.submit(101, {'d.txt': 'd1\n'})
        self.p4.interrupt_sync('@101', after=1)
        progress = self.interrupted_sync()
        self.assertEqual(progress['start'], {
                         'backend': 'git', 'commit': start, 'on-sync-branch': False})

    def test_a_detached_start_is_returned_to(self):
        self.git('switch', '-q', '--detach')
        start = self.git('rev-parse', 'HEAD')
        self.p4.submit(101, {'d.txt': 'd1\n'})
        self.sync()
        self.assert_at('', start)
        self.assertEqual(self.git('log', '-1', '--format=%s',
                         'main'), 'git-p4son: p4 sync //myclient/...@101')

    def test_the_setting_names_the_sync_branch(self):
        self.git('branch', 'p4')
        self.config('[sync]\nbranch = "p4"\n')
        main = self.git('rev-parse', 'main')
        self.p4.submit(101, {'d.txt': 'd1\n'})
        self.sync()
        self.assert_at('main', main)
        self.assertEqual(self.git('log', '-1', '--format=%s', 'p4'),
                         'git-p4son: p4 sync //myclient/...@101')

    def test_the_flag_overrides_the_setting(self):
        self.git('branch', 'p4')
        self.config('[sync]\nbranch = "nope"\n')
        self.p4.submit(101, {'d.txt': 'd1\n'})
        self.sync('--branch', 'p4')
        self.assertEqual(self.git('log', '-1', '--format=%s', 'p4'),
                         'git-p4son: p4 sync //myclient/...@101')

    def test_without_the_setting_or_main_it_refuses(self):
        self.git('branch', '-m', 'main', 'trunk')
        self.p4.submit(101, {'d.txt': 'd1\n'})
        self.sync_fails(
            message=r'Set \[sync\] branch in .git-p4son/config.toml, or run git p4son init')
        self.assertEqual(self.p4.synced(), [])

    def test_a_missing_sync_branch_is_refused(self):
        self.sync_fails('--branch', 'nope',
                        message='Sync branch "nope" does not exist')

    def test_merged_back_local_changes_keep_you_on_the_sync_branch(self):
        self.edit('a.txt', 'local\na1\n')
        self.commit('local change')
        self.git('switch', '-q', '-c', 'other')
        self.p4.submit(101, {'a.txt': 'a1\nupstream\n'})
        out = self.sync()
        self.assertEqual(self.branch(), 'main')
        self.assertEqual(self.read('a.txt'), 'local\na1\nupstream\n')
        self.assertIn(
            'Staying on main, with the local changes above to review and commit', out)

    def test_a_failing_pre_sync_hook_returns_you(self):
        self.commit_feature()
        feature = self.git('rev-parse', 'HEAD')
        self.hook('pre-sync', 'raise SystemExit(1)\n')
        self.p4.submit(101, {'d.txt': 'd1\n'})
        self.sync_fails(message='pre-sync hook failed')
        self.assert_at('feature', feature)
        self.assertEqual(self.p4.synced(), [])

    def test_hooks_see_the_sync_branch(self):
        self.commit_feature()
        path, read_log = self.hook_log()
        code = (f'import subprocess\nopen({path!r}, "a").write(subprocess.run(["git", "branch", "--show-current"], '
                'capture_output=True, text=True).stdout)\n')
        self.hook('pre-sync', code)
        self.hook('post-sync', code)
        self.p4.submit(101, {'d.txt': 'd1\n'})
        self.sync()
        self.assertEqual(read_log(), ['main', 'main'])

    def test_a_tag_named_like_the_sync_branch_is_not_read(self):
        self.p4.submit(101, {'d.txt': 'd1\n'})
        self.sync('101')
        self.git('tag', 'main', 'HEAD~1')
        self.p4.submit(102, {'d.txt': 'd2\n'})
        self.sync()
        self.assertEqual(self.synced_commits(), [101, 102])

    def test_an_untracked_file_left_by_a_post_sync_hook_does_not_keep_you(self):
        self.commit_feature()
        self.hook(
            'post-sync', f'open({os.path.join(self.ws, "left.txt")!r}, "w").write("x")\n')
        self.p4.submit(101, {'d.txt': 'd1\n'})
        self.sync()
        self.assertEqual(self.branch(), 'feature')

    def test_a_failure_partway_leaves_you_on_the_sync_branch(self):
        self.commit_feature()
        for change in (101, 102):
            self.p4.submit(change, {'d.txt': f'd{change}\n'})
        real = self.p4.stream

        def fail_second_sync(command, *args, **kwargs):
            if command[-1].endswith('@102') and '-n' not in command:
                raise P4SyncAbortError('cannot replace d.txt')
            return real(command, *args, **kwargs)
        start_patch(self, 'git_p4son.perforce.run_with_stream_output',
                    side_effect=fail_second_sync)
        resumable = 'Aborting sync.*then run "git p4son sync" to finish'
        with captured_output() as out, self.assertRaisesRegex(CommandError, resumable), \
                self.command_args(['sync', '101', '102']) as args:
            sync_command(args)
        self.assertEqual(self.branch(), 'main')
        self.assertEqual(self.synced_commits(), [101])
        self.assertIn('Staying on main, with the files as Perforce left them. Run "git p4son sync" to finish it, '
                      'which returns to feature.', out.getvalue())


class TestSyncHooks(SyncTestCase):
    def test_pre_sync_hooks_run_once_for_a_sequence(self):
        path, read_log = self.hook_log()
        self.hook('pre-sync', f'open({path!r}, "a").write("pre ")\n')
        self.hook('post-sync', f'open({path!r}, "a").write("post ")\n')
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.p4.submit(102, {'a.txt': 'a3\n'})
        self.sync('101', '102')
        self.assertEqual(read_log(), ['pre', 'post'])

    def test_hooks_are_told_the_backend(self):
        path, read_log = self.hook_log()
        code = f'import sys\nopen({path!r}, "a").write(sys.argv[1] + " ")\n'
        self.hook('pre-sync', code, '$(backend)')
        self.hook('post-sync', code, '$(backend)')
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.sync('101')
        backend = self.backend_name or 'git'
        self.assertEqual(read_log(), [backend, backend])

    def test_a_hook_can_run_git_p4son(self):
        """Under the lock of the sync that runs it, where a command of its own would find the workspace busy."""
        self.hook('pre-sync', 'import subprocess, sys\n'
                  'raise SystemExit(subprocess.call([sys.executable, "-m", "git_p4son", "--backend", "git", '
                  '"alias", "list"]))\n')
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.p4son('sync', '101')
        self.assertEqual(self.p4.synced(), ['@101'])

    def test_last_synced_runs_the_hooks_of_a_finished_sync_again(self):
        """As after a kill cut the post-sync hooks short: a sync with nothing new runs none."""
        path, read_log = self.hook_log()
        self.hook('post-sync', f'open({path!r}, "a").write("post ")\n')
        self.sync('last-synced')
        self.assertEqual(read_log(), ['post'])

    def test_failing_pre_sync_hook_aborts_before_syncing(self):
        self.hook('pre-sync', 'raise SystemExit(1)\n')
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.sync_fails(message='pre-sync hook failed')
        self.assertEqual(self.p4.synced(), [])

    def test_hooks_get_the_changelists_and_the_depot_root(self):
        path, read_log = self.hook_log()
        code = f'import sys\nopen({path!r}, "a").write(" ".join(sys.argv[1:]) + " ")\n'
        self.hook('pre-sync', code, 'pre', '$(last-synced)', '$(depot-root)')
        self.hook('post-sync', code, 'post', '$(last-synced)', '$(synced)')
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.p4.submit(102, {'a.txt': 'a3\n'})
        self.sync('101', '102')
        self.assertEqual(
            read_log(), ['pre', '100', '//myclient', 'post', '100', '102'])

    def test_hooks_run_from_the_workspace_root_wherever_sync_runs(self):
        path, read_log = self.hook_log()
        self.hook(
            'post-sync', f'import os\nopen({path!r}, "a").write(os.getcwd() + " ")\n')
        self.write_file(os.path.join('sub', 'x.txt'), 'x\n')
        self.git('add', 'sub')
        self.git('commit', '-q', '-m', 'Add sub')
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.run_command(
            sync_command, ['sync', '101'], invocation_dir=os.path.join(self.ws, 'sub'))
        self.assertTrue(os.path.samefile(read_log()[0], self.ws))

    def test_an_old_hook_directory_stops_the_sync_first(self):
        old = self.write_file(os.path.join(
            '.git-p4son', 'hooks', 'pre-sync', 'check.py'), 'print("old")\n')
        os.chmod(old, stat.S_IRWXU)
        self.p4.submit(101, {'a.txt': 'a2\n'})
        for argv in (['101'], ['-n', '101']):
            with self.subTest(argv=argv):
                self.sync_fails(
                    *argv, message=r'Hooks are configured in \[hooks\]')
        self.assertEqual(self.p4.synced(), [])

    @unittest.skipUnless(sys.platform == 'win32', 'Windows PowerShell')
    def test_powershell_hook_runs(self):
        path, read_log = self.hook_log()
        # PowerShell escapes a quote inside single quotes by doubling it.
        quoted = path.replace("'", "''")
        self.write_file(os.path.join('.git-p4son', 'hook.ps1'),
                        f"Add-Content -Path '{quoted}' -Value 'ps1'\n")
        save_config(self.ws, {'hooks': {'post-sync': [[
            'powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', '.git-p4son/hook.ps1']]}})
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
        self.sync_fails('-n', '50', message='without --allow-backwards')


class TestSyncTargets(SyncTestCase):
    def setUp(self):
        super().setUp()
        for change in (101, 102, 103):
            self.p4.submit(change, {'a.txt': f'a{change}\n'})

    def set_default_target(self, name):
        save_config(self.ws, {'sync': {'default-target': name}})

    def test_syncs_to_the_changelist_the_command_prints(self):
        self.write_sync_target('latest-safe', '102\n')
        out = self.sync('latest-safe')
        self.assertEqual(self.synced_commits(), [102])
        self.assertIn('Syncing to latest-safe CL (102)', out)

    def test_the_command_gets_the_last_synced_changelist_and_the_depot_root(self):
        self.write_sync_target('latest-safe', '102', command=[
            sys.executable, '.git-p4son/target-latest-safe.py', '$(last-synced)', '$(depot-root)'])
        self.sync('latest-safe')
        self.assertEqual(self.sync_target_runs('latest-safe')
                         [0]['args'], ['100', '//myclient'])

    def test_a_target_anywhere_in_a_sequence(self):
        self.write_sync_target('latest-safe', '102')
        self.sync('101', 'latest-safe', 'head')
        self.assertEqual(self.synced_commits(), [101, 102, 103])

    def test_a_target_out_of_order_is_named(self):
        self.write_sync_target('latest-safe', '101')
        self.sync_fails('102', 'latest-safe',
                        message=r'strictly increasing, got 102 then 101 \(latest-safe\)')
        self.assertEqual(self.p4.synced(), [])

    def test_a_target_named_twice_runs_once(self):
        self.write_sync_target('latest-safe', '102')
        self.sync_fails('latest-safe', 'Latest-Safe',
                        message='strictly increasing')
        self.assertEqual(len(self.sync_target_runs('latest-safe')), 1)

    def test_an_older_changelist_needs_allow_backwards(self):
        self.p4.submit(50, {'a.txt': 'old\n'})
        self.write_sync_target('latest-safe', '50')
        self.sync_fails('latest-safe', message='without --allow-backwards')
        self.sync('latest-safe', '--allow-backwards')
        self.assertEqual(self.synced_commits(), [50])

    def test_the_last_synced_changelist_is_already_there(self):
        self.write_sync_target('latest-safe', '100')
        out = self.sync('latest-safe')
        self.assertIn('Already at CL 100, nothing to sync', out)
        self.assertEqual(self.p4.synced(), [])

    def test_a_failing_command_syncs_nothing(self):
        self.write_sync_target('latest-safe', '', exit_code=1)
        self.sync_fails('101', 'latest-safe',
                        message='Sync target latest-safe failed, with exit code 1')
        self.assertEqual(self.p4.synced(), [])
        self.assertEqual(self.synced_commits(), [])

    def test_a_dry_run_runs_the_command(self):
        self.write_sync_target('latest-safe', '102')
        out = self.sync('-n', '101', 'latest-safe')
        self.assertIn('101 102', out)
        self.assertEqual(self.p4.synced(), [])

    def test_changelist_numbers_leave_an_invalid_target_alone(self):
        self.write_config(self.read('.git-p4son/config.toml') +
                          '[sync.targets]\nhead = "x"\n')
        self.sync('101')
        self.assertEqual(self.synced_commits(), [101])

    def test_a_bare_sync_resolves_the_default_target(self):
        self.write_sync_target('latest-safe', '102')
        self.set_default_target('latest-safe')
        self.sync()
        self.assertEqual(self.synced_commits(), [102])

    def test_head_still_syncs_to_the_latest_changelist(self):
        self.write_sync_target('latest-safe', '102')
        self.set_default_target('latest-safe')
        self.sync('head')
        self.assertEqual(self.synced_commits(), [103])
        self.assertEqual(self.sync_target_runs('latest-safe'), [])

    def test_a_default_that_is_not_a_target(self):
        self.set_default_target('latest-safe')
        self.sync_fails(message='which is not a sync target')
        self.assertEqual(self.p4.synced(), [])


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
        self.assertEqual(self.p4.synced(), ['@106'])

    def test_changelists_outside_the_view_are_not_split(self):
        self.p4.submit(107, {}, user='me')
        self.p4.submit(108, {'a.txt': 'a108\n'})
        self.configure('me')
        self.sync()
        self.assertEqual(self.synced_commits(), [101, 102, 104, 105, 108])

    def test_dry_run_prints_the_split_sequence(self):
        self.configure('me')
        out = self.sync('-n')
        self.assertIn('101 102 104 105 106', out)
        self.assertEqual(self.p4.synced(), [])


class TestSyncPreflight(unittest.TestCase):
    """The preflight checks blocking processes, then the git and the Perforce workspaces, in that order."""

    def preflight(self, ignore_blocking_processes=False):
        sync_preflight('//ws', '/ws', ignore_blocking_processes,
                       GitBackend('/ws'), 'main')

    @mock.patch('git_p4son.sync.p4_get_opened_files')
    @mock.patch('git_p4son.lib.get_dirty_files')
    @mock.patch('git_p4son.sync.check_no_blocking_processes',
                side_effect=CommandError('blocked'))
    def test_blocking_process_stops_before_everything_else(self, mock_blocking, mock_dirty, mock_opened):
        with self.assertRaisesRegex(CommandError, 'blocked'):
            self.preflight()
        mock_blocking.assert_called_once_with('/ws', False)
        mock_dirty.assert_not_called()
        mock_opened.assert_not_called()

    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.lib.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.check_no_blocking_processes')
    def test_ignore_blocking_processes_is_passed_on(self, mock_blocking, _dirty, _opened):
        self.preflight(ignore_blocking_processes=True)
        mock_blocking.assert_called_once_with('/ws', True)

    @mock.patch('git_p4son.sync.p4_get_opened_files')
    @mock.patch('git_p4son.lib.get_dirty_files', return_value=[('a.txt', 'modify')])
    def test_dirty_git_stops_before_p4(self, _dirty, mock_opened):
        with self.assertRaisesRegex(CommandError, 'not clean'):
            self.preflight()
        mock_opened.assert_not_called()

    @mock.patch('git_p4son.sync.get_tracked_files', return_value={'a.txt'})
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[('a.txt', 'modify')])
    @mock.patch('git_p4son.lib.get_dirty_files', return_value=[])
    def test_p4_opened_files_the_sync_branch_tracks_fail_it(self, _dirty, _opened, mock_tracked):
        with self.assertRaisesRegex(CommandError, 'p4-opened files tracked by git'):
            self.preflight()
        mock_tracked.assert_called_once_with(['a.txt'], '/ws', 'main')


_STRAY_FILE = 'jj makes a stray file part of the working change, which sync then rebases instead of refusing'
TestSyncOnJj = on_jj(
    TestSync, skip={'test_dirty_git_workspace_fails_before_syncing': _STRAY_FILE})
TestSyncLocalChangesOnJj = on_jj(TestSyncLocalChanges)
TestSyncProgressOnJj = on_jj(TestSyncProgress)
TestSyncResumeOnJj = on_jj(TestSyncResume)
TestSyncHooksOnJj = on_jj(TestSyncHooks)
TestSyncDryRunOnJj = on_jj(TestSyncDryRun)
TestSyncTargetsOnJj = on_jj(TestSyncTargets)
TestSyncSplittingOnJj = on_jj(TestSyncSplitting)

if __name__ == '__main__':
    unittest.main()
