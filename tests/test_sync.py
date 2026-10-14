"""Tests for git_p4son.sync module."""

import sys
import unittest
from unittest import mock

from git_p4son.cli import create_parser
from git_p4son.common import CommandError, RunError
from git_p4son.perforce import (
    ClobberMode,
    P4SyncClobberError,
    P4SyncAbortError,
    P4SyncOutputProcessor,
    get_latest_changelist,
    get_writable_files,
    p4_get_opened_files,
    parse_p4_sync_line,
)
from git_p4son.git import (
    add_all_files,
    commit,
    get_dirty_files,
)
from git_p4son.sync import (
    _collect_split_users,
    _make_synced_files_writable,
    _should_split,
    _sync_changelist,
    build_sync_targets,
    sync_command,
    sync_preflight,
)
from git_p4son.sync_merge import (
    ChangedFile,
    WritableSyncFileSet,
    prepare_writable_files,
)
from tests.helpers import make_changes, make_run_result, preview_update


def _sync_args(changelist, **overrides):
    """Parse a sync command line with the real CLI, then apply overrides like run_command does."""
    args = create_parser().parse_args(['sync', *changelist])
    args.workspace_dir = '/ws'
    args.invocation_dir = None
    for name, value in overrides.items():
        if not hasattr(args, name):
            raise AttributeError(f'sync has no argument {name!r}')
        setattr(args, name, value)
    if args.invocation_dir is None:
        args.invocation_dir = args.workspace_dir
    return args


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


class TestParseP4SyncLine(unittest.TestCase):
    def test_added_file(self):
        mode, filename = parse_p4_sync_line(
            '//depot/foo.txt#1 - added as /ws/foo.txt')
        self.assertEqual(mode, 'add')
        self.assertEqual(filename, '/ws/foo.txt')

    def test_deleted_file(self):
        mode, filename = parse_p4_sync_line(
            '//depot/foo.txt#2 - deleted as /ws/foo.txt')
        self.assertEqual(mode, 'del')
        self.assertEqual(filename, '/ws/foo.txt')

    def test_updated_file(self):
        mode, filename = parse_p4_sync_line(
            '//depot/foo.txt#3 - updating /ws/foo.txt')
        self.assertEqual(mode, 'upd')
        self.assertEqual(filename, '/ws/foo.txt')

    def test_clobber_file(self):
        mode, filename = parse_p4_sync_line(
            "Can't clobber writable file /ws/foo.txt")
        self.assertEqual(mode, 'clb')
        self.assertEqual(filename, '/ws/foo.txt')

    def test_unparsable_line(self):
        mode, filename = parse_p4_sync_line('some random output')
        self.assertIsNone(mode)
        self.assertIsNone(filename)


class TestGetWritableFiles(unittest.TestCase):
    def test_extracts_writable_files(self):
        stderr = [
            "Can't clobber writable file /ws/a.txt",
            "Can't clobber writable file /ws/b.txt",
            "some other error",
        ]
        result = get_writable_files(stderr)
        self.assertEqual(result, ['/ws/a.txt', '/ws/b.txt'])

    def test_empty_stderr(self):
        self.assertEqual(get_writable_files([]), [])


class TestGitGetDirtyFiles(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_clean_workspace(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=b'')
        self.assertEqual(get_dirty_files('/ws'), [])

    @mock.patch('git_p4son.git.run')
    def test_modified_file(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=b' M file.txt\0')
        result = get_dirty_files('/ws')
        self.assertEqual(result, [('file.txt', 'modify')])

    @mock.patch('git_p4son.git.run')
    def test_added_file(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=b'A  new.txt\0')
        result = get_dirty_files('/ws')
        self.assertEqual(result, [('new.txt', 'add')])

    @mock.patch('git_p4son.git.run')
    def test_deleted_file(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=b' D gone.txt\0')
        result = get_dirty_files('/ws')
        self.assertEqual(result, [('gone.txt', 'delete')])

    @mock.patch('git_p4son.git.run')
    def test_untracked_file(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=b'?? unknown.txt\0')
        result = get_dirty_files('/ws')
        self.assertEqual(result, [('unknown.txt', 'untracked')])

    @mock.patch('git_p4son.git.run')
    def test_multiple_files(self, mock_rwo):
        mock_rwo.return_value = make_run_result(
            stdout=b' M mod.txt\0A  add.txt\0?? new.txt\0')
        result = get_dirty_files('/ws')
        self.assertEqual(result, [
            ('mod.txt', 'modify'),
            ('add.txt', 'add'),
            ('new.txt', 'untracked'),
        ])

    @mock.patch('git_p4son.git.run')
    def test_rename_lists_the_new_path_once(self, mock_rwo):
        """A rename is "R  <new>\\0<old>\\0"; the old path is not a dirty file of its own."""
        mock_rwo.return_value = make_run_result(
            stdout=b'R  b "c".txt\0a.txt\0?? n\xc3\xb6.txt\0')
        self.assertEqual(get_dirty_files('/ws'), [
            ('b "c".txt', 'modify'), ('n\u00f6.txt', 'untracked')])

    @mock.patch('git_p4son.git.run')
    def test_command_failure(self, mock_rwo):
        mock_rwo.side_effect = RunError('git status failed')
        with self.assertRaises(RunError):
            get_dirty_files('/ws')


class TestP4GetOpenedFiles(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_clean(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        self.assertEqual(p4_get_opened_files('//depot', '/ws'), [])

    @mock.patch('git_p4son.perforce.run')
    def test_returns_workspace_relative_client_path(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/foo.txt',
            '... path /ws/foo.txt',
            '... action edit',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('foo.txt', 'modify')])

    @mock.patch('git_p4son.perforce.run')
    def test_add(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/new.txt',
            '... path /ws/new.txt',
            '... action add',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('new.txt', 'add')])

    @mock.patch('git_p4son.perforce.run')
    def test_delete(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/old.txt',
            '... path /ws/old.txt',
            '... action delete',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('old.txt', 'delete')])

    @mock.patch('git_p4son.perforce.run')
    def test_move_add(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/new.txt',
            '... path /ws/new.txt',
            '... action move/add',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('new.txt', 'add')])

    @mock.patch('git_p4son.perforce.run')
    def test_multiple_files(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/a.txt',
            '... path /ws/a.txt',
            '... action edit',
            '',
            '... depotFile //depot/b.txt',
            '... path /ws/b.txt',
            '... action add',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [
            ('a.txt', 'modify'),
            ('b.txt', 'add'),
        ])

    @mock.patch('git_p4son.perforce.run')
    def test_normalizes_slashes(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/dir/foo.txt',
            r'... path C:\ws\dir\foo.txt',
            '... action add',
        ])
        result = p4_get_opened_files('//depot', r'C:\ws')
        self.assertEqual(result, [('dir/foo.txt', 'add')])

    @mock.patch('git_p4son.perforce.run')
    def test_command_failure(self, mock_run):
        mock_run.side_effect = RunError('p4 opened failed')
        with self.assertRaises(RunError):
            p4_get_opened_files('//depot', '/ws')


class TestGitAddAllFiles(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_success(self, mock_run):
        mock_run.return_value = make_run_result()
        add_all_files('/ws')

    @mock.patch('git_p4son.git.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = RunError('git add failed')
        with self.assertRaises(RunError):
            add_all_files('/ws')


class TestGitCommit(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_success(self, mock_run):
        mock_run.return_value = make_run_result()
        commit('msg', '/ws')
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd, ['git', 'commit', '-m', 'msg'])

    @mock.patch('git_p4son.git.run')
    def test_allow_empty(self, mock_run):
        mock_run.return_value = make_run_result()
        commit('msg', '/ws', allow_empty=True)
        cmd = mock_run.call_args[0][0]
        self.assertIn('--allow-empty', cmd)


class TestGetLatestChangelist(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_success(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... change 54321',
            '... time 1704067200',
            '... user user',
            '... client ws',
        ])
        cl = get_latest_changelist('//myclient', '/ws')
        self.assertEqual(cl, 54321)
        mock_run.assert_called_once_with(
            ['p4', '-L', 'en', '-ztag', 'changes', '-m1', '-s', 'submitted',
             '//myclient/...#head'], cwd='/ws')

    @mock.patch('git_p4son.perforce.run')
    def test_no_changes_found(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        with self.assertRaises(CommandError):
            get_latest_changelist('//myclient', '/ws')


class TestP4SyncOutputProcessor(unittest.TestCase):
    def test_tracks_added_file(self):
        processor = P4SyncOutputProcessor()
        processor('//depot/foo.txt#1 - added as /ws/foo.txt', sys.stdout)
        self.assertEqual(processor.stats['add'], 1)

    def test_tracks_deleted_file(self):
        processor = P4SyncOutputProcessor()
        processor('//depot/foo.txt#2 - deleted as /ws/foo.txt', sys.stdout)
        self.assertEqual(processor.stats['del'], 1)

    def test_up_to_date_message(self):
        processor = P4SyncOutputProcessor()
        processor('//...@12345 - file(s) up-to-date.', sys.stdout)
        self.assertEqual(sum(processor.stats.values()), 0)

    def test_failed_rename_aborts(self):
        processor = P4SyncOutputProcessor()
        with self.assertRaises(P4SyncAbortError):
            processor('rename: failed to rename D:\\ws\\a.uasset after 10 '
                      'attempts: Cannot create a file when that file '
                      'already exists.', sys.stderr)


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
        with mock.patch('git_p4son.sync.log') as mock_log, \
                self.assertRaises(CommandError) as ctx:
            _sync_changelist(12345, 'test', '//myclient',
                             '/ws', expected_clobber=set())
        mock_log.error.assert_called_once_with(
            'rename: failed to rename D:\\ws\\a.uasset')
        self.assertIn('partially synced', str(ctx.exception))

    def test_unexpected_clobber_raises(self, mock_p4_sync):
        mock_p4_sync.side_effect = self._clobber('/ws/a.txt')
        with self.assertRaises(P4SyncClobberError):
            _sync_changelist(12345, 'test', '//myclient', '/ws',
                             expected_clobber=set())


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
                           return_value=list((known or {}).values())), \
                mock.patch('git_p4son.sync_split_users.log'):
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

    @mock.patch('git_p4son.sync.log')
    def test_no_previous_sync(self, _log):
        self.assertFalse(_should_split(['me'], None, [(106, 'latest')]))

    @mock.patch('git_p4son.sync.log')
    def test_syncing_backwards(self, _log):
        self.assertFalse(_should_split(
            ['me'], self._last_synced, [(90, '90')]))


class TestSyncCommand(unittest.TestCase):

    _last_synced = 10000

    def setUp(self):
        # Default to no client spec, so these tests never shell out to p4.
        patcher = mock.patch('git_p4son.depot.get_client_spec',
                             return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        # Default the clean-workspace check in lib to clean.
        patcher = mock.patch('git_p4son.lib.get_dirty_files',
                             return_value=[])
        self.mock_workspace_dirty = patcher.start()
        self.addCleanup(patcher.stop)

    def _empty_prep(self):
        return WritableSyncFileSet()

    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.get_staged_files')
    @mock.patch('git_p4son.sync._sync_changelist')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview', return_value=[])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_sync_specific_cl(self, _depot, _p4clean, mock_last_synced,
                              _head, _preview, mock_prep, _p4sync,
                              mock_staged, _git_add, _git_commit):
        mock_last_synced.return_value = self._last_synced
        mock_prep.return_value = self._empty_prep()
        mock_staged.return_value = ['file.txt']
        args = _sync_args(changelist=['12345'],
                          force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.sync_merge.log')
    @mock.patch('git_p4son.sync.merge_changed_files')
    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.sync._sync_changelist', return_value=['/ws/edited.log'])
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview',
                return_value=[preview_update('/ws/edited.log')])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.depot.get_client_spec')
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_allwrite_reports_only_refused_ignored_files(
            self, _depot, mock_spec, _p4clean, mock_last_synced, _head,
            _preview, mock_prep, _p4sync, _git_clean, _add, _commit, _merge,
            mock_log):
        """With allwrite only the ignored files p4 refused are reported."""
        spec = mock.Mock(uses_crlf=False, clobber_mode=ClobberMode.ALLWRITE)
        spec.name = 'myclient'
        mock_spec.return_value = spec
        mock_last_synced.return_value = self._last_synced
        mock_prep.return_value = WritableSyncFileSet(
            ignored=['/ws/pristine.log', '/ws/edited.log'])
        args = _sync_args(changelist=['12345'], force=False,
                          workspace_dir='/ws')
        self.assertEqual(sync_command(args), 0)

        headings = [str(c.args[0]) for c in mock_log.heading.call_args_list]
        self.assertIn('Files not synced (git-ignored and locally modified)',
                      headings)
        infos = [str(c.args[0]) for c in mock_log.info.call_args_list]
        self.assertIn('edited.log', infos)
        self.assertNotIn('pristine.log', infos)

    @mock.patch('git_p4son.sync._make_synced_files_writable')
    @mock.patch('git_p4son.sync.is_writable_mode', return_value=True)
    @mock.patch('git_p4son.sync.merge_changed_files')
    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.sync._sync_changelist', return_value=[])
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview')
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_writable_mode_restores_files_from_every_pass(
            self, _depot, _p4clean, mock_last_synced, _head, mock_preview,
            mock_prep, _p4sync, _git_clean, _add, _commit, _merge, _mode,
            mock_restore):
        """Every pass feeds the writable restore, and all writable files go read-only first."""
        mock_last_synced.return_value = self._last_synced
        mock_preview.side_effect = [[preview_update('/ws/a.cpp')],
                                    [preview_update('/ws/b.cpp')]]
        mock_prep.side_effect = [self._empty_prep(), self._empty_prep()]
        args = _sync_args(changelist=['12345'], force=False,
                          workspace_dir='/ws')
        self.assertEqual(sync_command(args), 0)

        mock_restore.assert_called_once_with(['/ws/a.cpp', '/ws/b.cpp'],
                                             '/ws')
        for call in mock_prep.call_args_list:
            self.assertIs(call.kwargs['clobber_mode'], ClobberMode.NOCLOBBER)

    @mock.patch('git_p4son.depot.get_depot_root', return_value=None)
    def test_no_depot_root_aborts(self, _depot):
        args = _sync_args(changelist=['100'], force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.get_staged_files')
    @mock.patch('git_p4son.sync._sync_changelist')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview', return_value=[])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.depot.get_client_spec')
    @mock.patch('git_p4son.depot.get_depot_root',
                return_value='//$(workspace)/Engine')
    def test_workspace_placeholder_resolved(self, _depot, mock_spec,
                                            _p4clean, mock_last_synced, _head,
                                            mock_preview, mock_prep, _p4sync,
                                            mock_staged, _git_add,
                                            _git_commit):
        spec = mock.Mock()
        spec.name = 'real-client'
        spec.uses_crlf = False
        spec.clobber_mode = ClobberMode.NOCLOBBER
        mock_spec.return_value = spec
        mock_last_synced.return_value = self._last_synced
        mock_prep.return_value = self._empty_prep()
        mock_staged.return_value = ['file.txt']
        args = _sync_args(changelist=['12345'],
                          force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        # $(workspace) is resolved before any p4 command runs against the depot.
        self.assertEqual(
            mock_preview.call_args.args[1], '//real-client/Engine')

    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.depot.get_client_spec', return_value=None)
    @mock.patch('git_p4son.depot.get_depot_root',
                return_value='//$(workspace)/Engine')
    def test_workspace_placeholder_without_client_spec_aborts(
            self, _depot, _spec, _p4clean):
        args = _sync_args(changelist=['100'], force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.git_last_synced_changelist', return_value=None)
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_dirty_git_workspace_aborts(self, _depot, _last_synced):
        self.mock_workspace_dirty.return_value = [('file.txt', 'modify')]
        args = _sync_args(changelist=['100'], force=False, workspace_dir='/ws')
        with self.assertRaisesRegex(CommandError, 'not clean'):
            sync_command(args)

    @mock.patch('git_p4son.sync.git_last_synced_changelist', return_value=None)
    @mock.patch('git_p4son.sync.get_tracked_files', return_value={'foo.txt'})
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.sync.p4_get_opened_files',
                return_value=[('foo.txt', 'modify')])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_dirty_p4_workspace_aborts(self, _depot, _p4clean, _git_clean,
                                       _tracked, _last_synced):
        args = _sync_args(changelist=['100'], force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.get_staged_files')
    @mock.patch('git_p4son.sync._sync_changelist')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview', return_value=[])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.get_tracked_files', return_value=set())
    @mock.patch('git_p4son.sync.p4_get_opened_files',
                return_value=[('ignored.bin', 'modify')])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_untracked_p4_opened_files_are_allowed(
            self, _depot, _p4clean, _tracked, mock_last_synced,
            _head, _preview, mock_prep, _p4sync,
            mock_staged, _add, _commit):
        mock_last_synced.return_value = 100
        mock_prep.return_value = self._empty_prep()
        mock_staged.return_value = []
        args = _sync_args(changelist=['200'], force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_older_cl_without_force_aborts(self, _depot, _git_clean,
                                           _p4clean, mock_last_synced, _head):
        mock_last_synced.return_value = 200
        args = _sync_args(changelist=['100'], force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.get_staged_files')
    @mock.patch('git_p4son.sync._sync_changelist')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview', return_value=[])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_older_cl_with_force_proceeds(self, _depot, _p4clean,
                                          mock_last_synced, _head, _preview,
                                          mock_prep, _p4sync,
                                          mock_staged, _add, _commit):
        mock_last_synced.return_value = 200
        mock_prep.return_value = self._empty_prep()
        mock_staged.return_value = ['file.txt']
        args = _sync_args(changelist=['100'], force=True, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.sync.log')
    @mock.patch('git_p4son.sync.run_hooks')
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.lib.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_same_cl_is_noop(self, _depot, mock_git_clean, mock_p4clean,
                             mock_last_synced, _head, mock_run_hooks, mock_log):
        mock_last_synced.return_value = 100
        args = _sync_args(changelist=['100'], force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        # Nothing to sync, so the whole preflight is skipped, not merely the hooks.
        mock_git_clean.assert_not_called()
        mock_p4clean.assert_not_called()
        mock_run_hooks.assert_not_called()
        mock_log.heading.assert_any_call('Skipping post-sync hooks')

    @mock.patch('git_p4son.sync.run_hooks')
    @mock.patch('git_p4son.sync._sync_changelist')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview',
                return_value=[preview_update('/ws/a.txt')])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_last_synced(self, _depot, _git_clean, _p4clean,
                         mock_last_synced, _head, _preview, mock_prep,
                         mock_p4sync, mock_run_hooks):
        mock_last_synced.return_value = 100
        mock_prep.return_value = self._empty_prep()
        mock_p4sync.return_value = None
        mock_run_hooks.return_value = []
        args = _sync_args(changelist=['last-synced'], force=False,
                          workspace_dir='/ws', invocation_dir='/invoked')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        mock_p4sync.assert_called_once_with(
            100, 'last synced', '//myclient', '/ws',
            expected_clobber=set())
        mock_run_hooks.assert_any_call('pre-sync', '/ws', '/invoked')
        mock_run_hooks.assert_any_call('post-sync', '/ws', '/invoked')

    @mock.patch('git_p4son.sync._make_synced_files_writable')
    @mock.patch('git_p4son.sync.run_hooks', return_value=[])
    @mock.patch('git_p4son.sync._sync_changelist', return_value=[])
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview',
                return_value=[preview_update('/ws/a.txt')])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_last_synced_restores_writable_files(
            self, _depot, _git_clean, _p4clean, mock_last_synced, _head,
            _preview, mock_prep, _p4sync, _hooks, mock_restore):
        mock_last_synced.return_value = 100
        mock_prep.return_value = self._empty_prep()
        args = _sync_args(changelist=['last-synced'], force=False,
                          workspace_dir='/ws', invocation_dir='/invoked')
        self.assertEqual(sync_command(args), 0)
        mock_restore.assert_called_once_with(['/ws/a.txt'], '/ws')

    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.merge_changed_files')
    @mock.patch('git_p4son.sync.run_hooks', return_value=[])
    @mock.patch('git_p4son.sync._sync_changelist', return_value=[])
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview',
                return_value=[preview_update('/ws/a.txt')])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_last_synced_merges_local_changes_without_committing(
            self, _depot, _git_clean, _p4clean, mock_last_synced, _head,
            _preview, mock_prep, _p4sync, _hooks, mock_merge, mock_commit):
        """last-synced is a catch-up pass: local changes are merged back and nothing is committed."""
        mock_last_synced.return_value = 100
        changed = ChangedFile(filepath='/ws/a.txt', base_commit='abc',
                              ours_path='/tmp/a.ours', base_path='/tmp/a.base')
        mock_prep.return_value = WritableSyncFileSet(changed=[changed])
        args = _sync_args(changelist=['last-synced'], force=False,
                          workspace_dir='/ws', invocation_dir='/invoked')
        self.assertEqual(sync_command(args), 0)
        mock_merge.assert_called_once()
        self.assertEqual(mock_merge.call_args.args[0], [changed])
        mock_commit.assert_not_called()

    @mock.patch('git_p4son.sync.run_hooks')
    @mock.patch('git_p4son.sync._sync_changelist')
    @mock.patch('git_p4son.sync.p4_sync_preview', return_value=[])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_last_synced_skips_sync_when_preview_empty(
            self, _depot, _git_clean, _p4clean, mock_last_synced,
            _head, _preview, mock_p4sync, mock_run_hooks):
        mock_last_synced.return_value = 100
        mock_run_hooks.return_value = []
        args = _sync_args(changelist=['last-synced'], force=False,
                          workspace_dir='/ws', invocation_dir='/invoked')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        mock_p4sync.assert_not_called()
        mock_run_hooks.assert_any_call('pre-sync', '/ws', '/invoked')
        mock_run_hooks.assert_any_call('post-sync', '/ws', '/invoked')

    @mock.patch('git_p4son.sync.run_hooks')
    @mock.patch('git_p4son.sync._sync_changelist')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview',
                return_value=[preview_update('/ws/a.txt')])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_failing_pre_sync_hook_aborts(
            self, _depot, _git_clean, _p4clean, mock_last_synced, _head,
            _preview, mock_prep, mock_p4sync, mock_run_hooks):
        """A non-zero pre-sync hook aborts before any changelist is synced."""
        mock_last_synced.return_value = 100
        mock_prep.return_value = self._empty_prep()
        mock_run_hooks.return_value = [make_run_result(returncode=1)]
        args = _sync_args(changelist=['200'], force=False,
                          workspace_dir='/ws', invocation_dir='/invoked')
        rc = sync_command(args)
        self.assertEqual(rc, 1)
        mock_p4sync.assert_not_called()
        mock_run_hooks.assert_called_once_with('pre-sync', '/ws', '/invoked')

    @mock.patch('git_p4son.sync.merge_changed_files')
    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.run_hooks')
    @mock.patch('git_p4son.sync._sync_changelist')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview',
                return_value=[preview_update('/ws/a.txt')])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_pre_sync_hook_runs_once_for_multiple_changelists(
            self, _depot, _git_clean, _p4clean, mock_last_synced, _head,
            _preview, mock_prep, mock_p4sync, mock_run_hooks, _add, _commit,
            _merge):
        """Pre-sync hooks run once, not per changelist, for a CL sequence."""
        mock_last_synced.return_value = 100
        mock_prep.return_value = self._empty_prep()
        mock_run_hooks.return_value = []
        args = _sync_args(changelist=['150', '200'], force=False,
                          workspace_dir='/ws', invocation_dir='/invoked')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        pre_sync_calls = [
            call for call in mock_run_hooks.call_args_list
            if call.args[0] == 'pre-sync'
        ]
        self.assertEqual(len(pre_sync_calls), 1)

    @mock.patch('git_p4son.sync.add_all_files', mock.Mock())
    @mock.patch('git_p4son.sync.get_latest_changelist')
    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.get_staged_files')
    @mock.patch('git_p4son.sync._sync_changelist')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview', return_value=[])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_latest_keyword(self, _depot, _p4clean, mock_last_synced, _head,
                            _preview, mock_prep, _p4sync,
                            mock_staged, _commit, mock_get_latest):
        mock_last_synced.return_value = 100
        mock_get_latest.return_value = 200
        mock_prep.return_value = self._empty_prep()
        mock_staged.return_value = []
        args = _sync_args(changelist=[], force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.sync.add_all_files', mock.Mock())
    @mock.patch('git_p4son.sync.get_latest_changelist')
    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.get_staged_files')
    @mock.patch('git_p4son.sync._sync_changelist')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview', return_value=[])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_explicit_head_keyword(self, _depot, _p4clean, mock_last_synced,
                                   _head, _preview, mock_prep, _p4sync,
                                   mock_staged, _commit, mock_get_latest):
        """An explicit "head" syncs to the latest changelist."""
        mock_last_synced.return_value = 100
        mock_get_latest.return_value = 200
        mock_prep.return_value = self._empty_prep()
        mock_staged.return_value = []
        args = _sync_args(changelist=['head'],
                          force=False, workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        mock_get_latest.assert_called_once()

    @mock.patch('git_p4son.sync.merge_changed_files')
    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.sync._sync_changelist')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview',
                return_value=[preview_update('/ws/a.txt')])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_multiple_changelists_commit_each(
            self, _depot, _p4clean, mock_last_synced, _head, _preview, mock_prep,
            mock_p4sync, _git_clean, _add, mock_commit, _merge):
        """Multiple changelists sync in sequence after a catch-up pass, one commit each."""
        mock_last_synced.return_value = 100
        mock_prep.return_value = self._empty_prep()
        args = _sync_args(changelist=['123', '156', '178'], force=False,
                          workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)

        # One commit per specified changelist, in order.
        commit_msgs = [c.args[0] for c in mock_commit.call_args_list]
        self.assertEqual(commit_msgs, [
            'git-p4son: p4 sync //myclient/...@123',
            'git-p4son: p4 sync //myclient/...@156',
            'git-p4son: p4 sync //myclient/...@178',
        ])
        # Catch-up to the last synced CL, then each target in order.
        synced_cls = [c.args[0] for c in mock_p4sync.call_args_list]
        self.assertEqual(synced_cls, [100, 123, 156, 178])
        # Local changes are merged back exactly once, after all commits.
        _merge.assert_called_once()

    @mock.patch('git_p4son.sync.merge_changed_files')
    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.sync._sync_changelist')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview',
                return_value=[preview_update('/ws/a.txt')])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_explicit_last_synced_in_list_is_skipped(
            self, _depot, _p4clean, mock_last_synced, _head, _preview, mock_prep,
            mock_p4sync, _git_clean, _add, mock_commit, _merge):
        """A specified changelist equal to the last synced one is skipped."""
        mock_last_synced.return_value = 100
        mock_prep.return_value = self._empty_prep()
        args = _sync_args(changelist=['100', '156'], force=False,
                          workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        commit_msgs = [c.args[0] for c in mock_commit.call_args_list]
        self.assertEqual(commit_msgs,
                         ['git-p4son: p4 sync //myclient/...@156'])

    @mock.patch('git_p4son.sync.merge_changed_files')
    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.sync._sync_changelist')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview',
                return_value=[preview_update('/ws/a.txt')])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_older_changelist_in_list_synced_with_force(
            self, _depot, _p4clean, mock_last_synced, _head, _preview, mock_prep,
            mock_p4sync, _git_clean, _add, mock_commit, _merge):
        """An older changelist is synced, not skipped, with --force."""
        mock_last_synced.return_value = 100
        mock_prep.return_value = self._empty_prep()
        args = _sync_args(changelist=['50', '156'], force=True,
                          workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        commit_msgs = [c.args[0] for c in mock_commit.call_args_list]
        self.assertEqual(commit_msgs, [
            'git-p4son: p4 sync //myclient/...@50',
            'git-p4son: p4 sync //myclient/...@156',
        ])

    @mock.patch('git_p4son.sync.merge_changed_files')
    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.sync._sync_changelist')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview',
                return_value=[preview_update('/ws/a.txt')])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_three_changelists_starting_backward_with_force(
            self, _depot, _p4clean, mock_last_synced, _head, _preview, mock_prep,
            mock_p4sync, _git_clean, _add, mock_commit, _merge):
        """With --force the first target may be older than the last synced changelist."""
        mock_last_synced.return_value = 100
        mock_prep.return_value = self._empty_prep()
        args = _sync_args(changelist=['50', '156', '178'], force=True,
                          workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        commit_msgs = [c.args[0] for c in mock_commit.call_args_list]
        self.assertEqual(commit_msgs, [
            'git-p4son: p4 sync //myclient/...@50',
            'git-p4son: p4 sync //myclient/...@156',
            'git-p4son: p4 sync //myclient/...@178',
        ])
        # Catch-up to the last synced CL, then each target in order.
        synced_cls = [c.args[0] for c in mock_p4sync.call_args_list]
        self.assertEqual(synced_cls, [100, 50, 156, 178])

    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_older_changelist_in_list_without_force_aborts(
            self, _depot, _git_clean, _p4clean, mock_last_synced, _head):
        mock_last_synced.return_value = 100
        args = _sync_args(changelist=['50', '156'], force=False,
                          workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_non_increasing_changelists_abort(
            self, _depot, _git_clean, _p4clean, mock_last_synced, _head):
        mock_last_synced.return_value = 100
        args = _sync_args(changelist=['156', '123'], force=False,
                          workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_duplicate_changelists_abort(
            self, _depot, _git_clean, _p4clean, mock_last_synced, _head):
        mock_last_synced.return_value = 100
        args = _sync_args(changelist=['123', '123'], force=False,
                          workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_last_synced_combined_with_number_aborts(
            self, _depot, _git_clean, _p4clean, mock_last_synced, _head):
        mock_last_synced.return_value = 100
        args = _sync_args(changelist=['last-synced', '123'], force=False,
                          workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_head_not_last_aborts(
            self, _depot, _git_clean, _p4clean, mock_last_synced, _head):
        mock_last_synced.return_value = 100
        args = _sync_args(changelist=['head', '123'], force=False,
                          workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.sync.get_latest_changelist', return_value=200)
    @mock.patch('git_p4son.sync.merge_changed_files')
    @mock.patch('git_p4son.sync.commit')
    @mock.patch('git_p4son.sync.add_all_files')
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.sync._sync_changelist')
    @mock.patch('git_p4son.sync.prepare_writable_files')
    @mock.patch('git_p4son.sync.p4_sync_preview',
                return_value=[preview_update('/ws/a.txt')])
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_trailing_head_resolves_and_syncs(
            self, _depot, _p4clean, mock_last_synced, _head, _preview, mock_prep,
            mock_p4sync, _git_clean, _add, mock_commit, _merge, _latest):
        """A trailing "head" resolves to the latest changelist and is synced last."""
        mock_last_synced.return_value = 100
        mock_prep.return_value = self._empty_prep()
        args = _sync_args(changelist=['123', '156', 'head'], force=False,
                          workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 0)
        commit_msgs = [c.args[0] for c in mock_commit.call_args_list]
        self.assertEqual(commit_msgs, [
            'git-p4son: p4 sync //myclient/...@123',
            'git-p4son: p4 sync //myclient/...@156',
            'git-p4son: p4 sync //myclient/...@200',
        ])

    @mock.patch('git_p4son.sync.get_latest_changelist', return_value=140)
    @mock.patch('git_p4son.sync.get_head_commit', return_value='def456')
    @mock.patch('git_p4son.sync.git_last_synced_changelist')
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.sync.get_staged_files', return_value=[])
    @mock.patch('git_p4son.depot.get_depot_root', return_value='//myclient')
    def test_trailing_head_below_preceding_cl_aborts(
            self, _depot, _git_clean, _p4clean, mock_last_synced, _head,
            _latest):
        """A "head" not above the preceding changelist fails the strictly increasing check."""
        mock_last_synced.return_value = 100
        args = _sync_args(changelist=['123', '156', 'head'], force=False,
                          workspace_dir='/ws')
        rc = sync_command(args)
        self.assertEqual(rc, 1)


class TestSyncDryRun(unittest.TestCase):
    """A dry run prints the sync sequence and stops before touching the workspaces."""

    def setUp(self):
        for target, value in (
                ('git_p4son.depot.get_client_spec', None),
                ('git_p4son.depot.get_depot_root', '//myclient'),
                ('git_p4son.sync.get_latest_changelist', 10500)):
            patcher = mock.patch(target, return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch('git_p4son.sync.git_last_synced_changelist',
                             return_value=10000)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch('git_p4son.sync.log')
        self.mock_log = patcher.start()
        self.addCleanup(patcher.stop)
        # Everything past the dry-run exit: none of it may run.
        self.untouched = {}
        for name in ('sync_preflight', 'run_hooks', 'get_head_commit',
                     '_sync_pass', 'commit'):
            patcher = mock.patch(f'git_p4son.sync.{name}')
            self.untouched[name] = patcher.start()
            self.addCleanup(patcher.stop)

    def _run(self, changelist, force=False):
        args = _sync_args(changelist=changelist, force=force,
                          workspace_dir='/ws', dry_run=True)
        return sync_command(args)

    def _sequence(self):
        """The line logged under the "Sync sequence" heading."""
        self.mock_log.heading.assert_any_call('Sync sequence')
        return self.mock_log.success.call_args_list[-1].args[0]

    def _assert_nothing_run(self):
        for name, patched in self.untouched.items():
            with self.subTest(name=name):
                patched.assert_not_called()

    def test_latest(self):
        self.assertEqual(self._run([]), 0)
        self.assertEqual(self._sequence(), '10500')
        self._assert_nothing_run()

    def test_sequence_ending_in_head(self):
        self.assertEqual(self._run(['10100', '10200', 'head']), 0)
        self.assertEqual(self._sequence(), '10100 10200 10500')
        self._assert_nothing_run()

    def test_last_synced(self):
        self.assertEqual(self._run(['last-synced']), 0)
        self.assertEqual(self._sequence(), '10000 (last synced)')
        self._assert_nothing_run()

    def test_older_changelist_with_force(self):
        self.assertEqual(self._run(['9000'], force=True), 0)
        self.assertEqual(self._sequence(), '9000')
        self._assert_nothing_run()

    def test_invalid_arguments_still_fail(self):
        """A dry run validates the targets exactly as a real sync does."""
        self.assertEqual(self._run(['9000']), 1)
        self.mock_log.error.assert_called_once()
        self._assert_nothing_run()

    def test_already_synced(self):
        self.assertEqual(self._run(['10000']), 0)
        self.mock_log.info.assert_any_call('Already synced, nothing to do.')
        self._assert_nothing_run()


class TestSyncSplitting(unittest.TestCase):
    """sync splits out the configured split users unless --no-split, plus any given with -u."""

    # Last synced at 100, latest 106; "me" submitted 102 and 105.
    _changes = make_changes(
        (100, 'other'), (101, 'other'), (102, 'me'), (103, 'other'),
        (104, 'other'), (105, 'me'), (106, 'other'))

    def setUp(self):
        self.patches = {}
        for target, value in (
                ('git_p4son.depot.get_client_spec', None),
                ('git_p4son.depot.get_depot_root', '//myclient'),
                ('git_p4son.sync.git_last_synced_changelist',
                 100),
                ('git_p4son.sync.get_latest_changelist', 106),
                ('git_p4son.sync.get_split_users', []),
                ('git_p4son.sync.get_submitted_changes', self._changes),
                ('git_p4son.sync_split_users.get_p4_user', 'me'),
                ('git_p4son.sync_split_users.get_existing_p4_users', []),
                ('git_p4son.sync.sync_preflight', True),
                ('git_p4son.sync.get_head_commit', 'def456'),
                ('git_p4son.sync._sync_pass', WritableSyncFileSet()),
                ('git_p4son.sync.get_staged_files', []),
                ('git_p4son.sync.add_all_files', None),
                ('git_p4son.sync.commit', None),
                ('git_p4son.sync.merge_changed_files', None),
                ('git_p4son.sync._make_synced_files_writable', None),
                ('git_p4son.sync.run_hooks', [])):
            patcher = mock.patch(target, return_value=value)
            self.patches[target.rsplit('.', 1)[1]] = patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch('git_p4son.sync.log')
        self.mock_log = patcher.start()
        self.addCleanup(patcher.stop)
        # User checks and resolution log from their own module.
        patcher = mock.patch('git_p4son.sync_split_users.log')
        self.mock_users_log = patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, *changelist, configured=(), split_user=None,
             no_split=False, force=False, dry_run=False):
        self.patches['get_split_users'].return_value = list(configured)
        args = _sync_args(changelist=list(changelist), force=force,
                          workspace_dir='/ws', dry_run=dry_run,
                          split_user=split_user, no_split=no_split)
        return sync_command(args)

    def _synced(self):
        """The (changelist, label) of each target pass, catch-up excluded."""
        calls = self.patches['_sync_pass'].call_args_list
        return [(c.args[0], c.args[1]) for c in calls
                if c.args[1] != 'last synced']

    def _assert_not_split(self):
        self.patches['get_submitted_changes'].assert_not_called()

    def test_configured_user_changelists_get_their_own_commits(self):
        self.assertEqual(self._run(configured=['me']), 0)
        self.assertEqual(self._synced(), [
            (101, 'split'), (102, 'split'), (104, 'split'), (105, 'split'),
            (106, 'latest')])
        self.patches['get_submitted_changes'].assert_called_once_with(
            '//myclient', 100, 106, '/ws')

    def test_no_split_users_makes_no_extra_queries(self):
        self.assertEqual(self._run(), 0)
        self.assertEqual(self._synced(), [(106, 'latest')])
        self._assert_not_split()
        self.patches['get_p4_user'].assert_not_called()
        self.patches['get_existing_p4_users'].assert_not_called()

    def test_placeholder_resolves_to_the_current_user(self):
        self.assertEqual(self._run(configured=['$(user)']), 0)
        self.assertEqual([cl for cl, _ in self._synced()],
                         [101, 102, 104, 105, 106])

    def test_unresolvable_placeholder_fails_without_syncing(self):
        self.patches['get_p4_user'].return_value = None
        with self.assertRaisesRegex(CommandError, '--no-split'):
            self._run(configured=['$(user)'])
        self.patches['_sync_pass'].assert_not_called()

    def test_explicit_sequence_is_merged_with_split_points(self):
        self.assertEqual(self._run('101', '103', 'head', configured=['me']),
                         0)
        self.assertEqual(self._synced(), [
            (101, 'specified'), (102, 'split'), (103, 'specified'),
            (104, 'split'), (105, 'split'), (106, 'latest')])

    def test_explicit_target_bounds_the_range(self):
        self.patches['get_submitted_changes'].return_value = (
            self._changes[:5])
        self.assertEqual(self._run('104', configured=['me']), 0)
        self.assertEqual(self._synced(), [
            (101, 'split'), (102, 'split'), (104, 'specified')])
        self.patches['get_submitted_changes'].assert_called_once_with(
            '//myclient', 100, 104, '/ws')
        self.patches['get_latest_changelist'].assert_not_called()

    def test_split_user_adds_to_the_configured_users(self):
        self.patches['get_existing_p4_users'].return_value = ['Other']
        self.assertEqual(self._run(configured=['me'], split_user=['other']),
                         0)
        # Everyone's changelists are split out, so every one is a target.
        self.assertEqual([cl for cl, _ in self._synced()],
                         [101, 102, 103, 104, 105, 106])
        self.patches['get_existing_p4_users'].assert_called_once_with(
            ['other'], '/ws')

    def test_unknown_split_user_fails_before_any_work(self):
        with self.assertRaisesRegex(CommandError,
                                    '^No such Perforce user: nobody$'):
            self._run(split_user=['nobody'])
        self.patches['get_latest_changelist'].assert_not_called()
        self.patches['sync_preflight'].assert_not_called()
        self.patches['_sync_pass'].assert_not_called()

    def test_quoted_placeholder_split_user_is_not_checked(self):
        self.assertEqual(self._run(split_user=['$(user)']), 0)
        self.patches['get_existing_p4_users'].assert_not_called()
        self.assertEqual([cl for cl, _ in self._synced()],
                         [101, 102, 104, 105, 106])

    def test_no_split_ignores_the_configured_users(self):
        self.assertEqual(self._run(configured=['me'], no_split=True), 0)
        self.assertEqual(self._synced(), [(106, 'latest')])
        self._assert_not_split()

    def test_no_split_with_split_user_splits_only_that_user(self):
        self.patches['get_existing_p4_users'].return_value = ['me']
        self.patches['get_submitted_changes'].return_value = make_changes(
            (100, 'other'), (101, 'other'), (102, 'me'), (103, 'bob'),
            (104, 'other'))
        self.patches['get_latest_changelist'].return_value = 104
        self.assertEqual(self._run(configured=['bob'], split_user=['me'],
                                   no_split=True), 0)
        self.assertEqual([cl for cl, _ in self._synced()], [101, 102, 104])

    def test_last_synced_is_not_split(self):
        self.assertEqual(self._run('last-synced', configured=['me']), 0)
        self._assert_not_split()

    def test_first_sync_is_not_split(self):
        self.patches['git_last_synced_changelist'].return_value = None
        self.assertEqual(self._run(configured=['me']), 0)
        self.assertEqual(self._synced(), [(106, 'latest')])
        self._assert_not_split()
        self.mock_log.info.assert_any_call(
            'Not splitting out changelists: no previous sync to start from')

    def test_syncing_backwards_is_not_split(self):
        self.assertEqual(self._run('90', configured=['me'], force=True), 0)
        self.assertEqual(self._synced(), [(90, 'specified')])
        self._assert_not_split()
        self.mock_log.info.assert_any_call(
            'Not splitting out changelists: syncing to an older changelist')

    def test_already_synced_makes_no_split_queries(self):
        self.patches['get_latest_changelist'].return_value = 100
        self.assertEqual(self._run(configured=['me']), 0)
        self._assert_not_split()
        self.patches['sync_preflight'].assert_not_called()

    def test_preflight_runs_before_the_split_queries(self):
        self.patches['sync_preflight'].return_value = False
        self.assertEqual(self._run(configured=['me']), 1)
        self._assert_not_split()
        self.patches['get_p4_user'].assert_not_called()

    def test_dry_run_prints_the_split_sequence(self):
        self.assertEqual(self._run(configured=['me'], dry_run=True), 0)
        self.mock_log.success.assert_any_call('101 102 104 105 106')
        self.mock_log.info.assert_any_call('Dry run, nothing synced.')
        self.patches['sync_preflight'].assert_not_called()
        self.patches['_sync_pass'].assert_not_called()


class TestSyncPreflight(unittest.TestCase):
    """The preflight runs blocking processes, workspace checks, then hooks, in that order."""

    @mock.patch('git_p4son.sync.run_hooks')
    @mock.patch('git_p4son.sync.p4_get_opened_files')
    @mock.patch('git_p4son.lib.get_dirty_files')
    @mock.patch('git_p4son.sync.check_no_blocking_processes',
                return_value=False)
    def test_blocking_process_stops_before_everything_else(
            self, mock_blocking, mock_dirty, mock_opened, mock_run_hooks):
        self.assertFalse(sync_preflight(
            '//ws', '/ws', '/invoked', ignore_blocking_processes=False))
        mock_blocking.assert_called_once_with('/ws', False)
        mock_dirty.assert_not_called()
        mock_opened.assert_not_called()
        mock_run_hooks.assert_not_called()

    @mock.patch('git_p4son.sync.run_hooks', return_value=[])
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.lib.get_dirty_files', return_value=[])
    @mock.patch('git_p4son.sync.check_no_blocking_processes',
                return_value=True)
    def test_ignore_blocking_processes_is_passed_on(
            self, mock_blocking, _dirty, _opened, _hooks):
        self.assertTrue(sync_preflight('//ws', '/ws', '/invoked',
                                       ignore_blocking_processes=True))
        mock_blocking.assert_called_once_with('/ws', True)

    @mock.patch('git_p4son.sync.run_hooks', return_value=[])
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.lib.get_dirty_files', return_value=[])
    def test_clean_workspaces_run_the_hooks(
            self, _dirty, _opened, mock_run_hooks):
        self.assertTrue(sync_preflight(
            '//ws', '/ws', '/invoked', ignore_blocking_processes=False))
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
        self.assertFalse(sync_preflight(
            '//ws', '/ws', '/invoked', ignore_blocking_processes=False))
        mock_run_hooks.assert_not_called()

    @mock.patch('git_p4son.sync.run_hooks',
                return_value=[make_run_result(returncode=1)])
    @mock.patch('git_p4son.sync.p4_get_opened_files', return_value=[])
    @mock.patch('git_p4son.lib.get_dirty_files', return_value=[])
    def test_failing_hook_fails_the_gate(self, _dirty, _opened, _hooks):
        self.assertFalse(sync_preflight(
            '//ws', '/ws', '/invoked', ignore_blocking_processes=False))


if __name__ == '__main__':
    unittest.main()
