"""Tests for git_p4son.git module."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from git_p4son.common import CommandError, run, RunError, RunResult
from git_p4son.git import (
    add_all_files,
    add_files,
    commit,
    find_base_commits,
    format_sync_subject,
    get_blob_oids,
    get_commit_lines_since,
    get_commits_since,
    get_commit_subjects_since,
    get_current_branch,
    get_dirty_files,
    get_files_at_commits,
    get_head_commit,
    get_head_subject,
    get_local_changes,
    _get_rebase_branch,
    get_staged_files,
    get_tracked_files,
    get_untracked_files,
    get_workspace_dir,
    git_last_synced_changelist,
    has_commits,
    is_workspace_dir,
    list_tracked_files,
    merge_file,
    parse_sync_subject,
    restore_files,
)
from tests.helpers import GitRepoTestCase, make_run_result


class TestControlCharactersInPaths(GitRepoTestCase):
    """-z output is read as bytes: text mode would turn a carriage return in a path into a newline."""

    NAMES = ['cr\rname.txt', 'trailing space.txt ']

    def _commit_names(self, content, message):
        for name in self.NAMES:
            self.write_file(name, content)
        self.commit(message)

    def test_names_come_back_verbatim(self):
        self._commit_names('base', 'git-p4son: p4 sync //ws/...@1')
        base = get_head_commit(self.ws)
        self._commit_names('changed', 'local change')

        self.assertEqual(sorted(list_tracked_files(self.ws)),
                         sorted(self.NAMES))
        self.assertEqual(sorted(get_local_changes(base, self.ws).mods),
                         sorted(self.NAMES))
        self.assertEqual(find_base_commits(self.NAMES, 'HEAD', self.ws),
                         dict.fromkeys(self.NAMES, base))

        for name in self.NAMES:
            self.write_file(name, 'dirty')
        self.assertEqual(sorted(name for name, _ in get_dirty_files(self.ws)),
                         sorted(self.NAMES))
        add_files(self.NAMES, self.ws)
        self.assertEqual(sorted(get_staged_files(self.ws)),
                         sorted(self.NAMES))


class TestRepoSetup(GitRepoTestCase):
    def test_has_commits(self):
        self.assertFalse(has_commits(self.ws))
        self.write_file('a.txt', 'a')
        self.commit()
        self.assertTrue(has_commits(self.ws))

    def test_add_files_stages_only_those(self):
        self.write_file('a.txt', 'a')
        self.write_file('b.txt', 'b')
        add_files(['a.txt'], self.ws)
        self.assertEqual(get_staged_files(self.ws), ['a.txt'])


class TestRestoreFiles(GitRepoTestCase):
    def test_restore_files_from_the_index(self):
        self.write_file('a.txt', 'git')
        self.write_file('b c.txt', 'git')
        self.commit()
        self.write_file('a.txt', 'depot')
        self.write_file('b c.txt', 'depot')
        restore_files(['a.txt', 'b c.txt'], self.ws, dry_run=False)
        for name in ('a.txt', 'b c.txt'):
            with open(os.path.join(self.ws, name)) as f:
                self.assertEqual(f.read(), 'git')


class TestGetTrackedFiles(GitRepoTestCase):
    def test_returns_tracked_subset(self):
        self.write_file('src/main.py', 'code')
        self.commit()
        self.write_file('untracked.log', '')

        result = get_tracked_files(
            ['src/main.py', 'untracked.log'], self.ws)
        self.assertEqual(result, {'src/main.py'})

    def test_tracked_file_matching_ignore_pattern_is_tracked(self):
        """A tracked file matching a .gitignore pattern is still tracked."""
        self.write_file('.gitignore', '*.ini\n')
        self.write_file('config.ini', 'tracked anyway')
        self.git('add', '-f', 'config.ini', '.gitignore')
        self.git('commit', '-m', 'add')
        self.write_file('other.ini', 'ignored and untracked')

        result = get_tracked_files(
            ['config.ini', 'other.ini'], self.ws)
        self.assertEqual(result, {'config.ini'})

    def test_absolute_paths_map_back_to_input(self):
        self.write_file('src/main.py', 'code')
        self.commit()
        abs_path = os.path.join(self.ws, 'src', 'main.py')

        result = get_tracked_files([abs_path], self.ws)
        self.assertEqual(result, {abs_path})

    def test_non_ascii_paths_match(self):
        """Non-ASCII paths match the input paths rather than coming back C-quoted."""
        name = 'bäck.py'
        self.write_file(name, 'code')
        self.commit()

        result = get_tracked_files([name], self.ws)
        self.assertEqual(result, {name})

    def test_one_listing_for_any_number_of_paths(self):
        for name in ('a.py', 'b.py'):
            self.write_file(name, name)
        self.commit()
        self.write_file('c.log', '')
        with mock.patch('git_p4son.git.run', wraps=run) as mock_run:
            result = get_tracked_files(
                ['a.py', 'b.py', 'c.log'] * 1000, self.ws)
        self.assertEqual(result, {'a.py', 'b.py'})
        mock_run.assert_called_once()

    def test_empty_input(self):
        result = get_tracked_files([], self.ws)
        self.assertEqual(result, set())


class TestGetStagedFiles(GitRepoTestCase):
    def test_lists_each_staged_file_including_new_folders(self):
        self.write_file('a.txt', 'A')
        self.write_file('b.txt', 'B')
        self.commit('initial')
        self.write_file('a.txt', 'A2')
        os.remove(os.path.join(self.ws, 'b.txt'))
        self.write_file('new/one.txt', '1')
        self.write_file('new/two.txt', '2')
        self.git('add', '.')

        self.assertEqual(sorted(get_staged_files(self.ws)),
                         ['a.txt', 'b.txt', 'new/one.txt', 'new/two.txt'])

    def test_nothing_staged(self):
        self.write_file('a.txt', 'A')
        self.commit('initial')
        self.assertEqual(get_staged_files(self.ws), [])


class TestListTrackedFiles(GitRepoTestCase):
    def test_lists_tracked_files_only(self):
        self.write_file('src/main.py', 'code')
        self.write_file('.gitignore', '*.log\n')
        self.commit()
        self.write_file('untracked.txt', '')
        self.write_file('build.log', '')

        self.assertEqual(sorted(list_tracked_files(self.ws)),
                         ['.gitignore', 'src/main.py'])

    def test_non_ascii_paths_are_not_quoted(self):
        self.write_file('src/Ärlig.py', 'code')
        self.commit()
        self.assertEqual(list_tracked_files(self.ws), ['src/Ärlig.py'])

    def test_empty_repo(self):
        self.assertEqual(list_tracked_files(self.ws), [])


class TestRevision(GitRepoTestCase):
    """Reading the change from a revision that is not HEAD, as in a jj workspace."""

    def build_workspace(self):
        super().build_workspace()
        self.base, self.tip = self.commit_history(
            ('base', {'a.txt': 'a', 'b.txt': 'b'}),
            ('change', {'a.txt': 'a2', 'b.txt': None, 'c.txt': 'c'}))
        self.git('reset', '-q', '--hard', self.base)

    def test_local_changes(self):
        changes = get_local_changes(self.base, self.ws, self.tip)
        self.assertEqual((changes.adds, changes.mods, changes.dels),
                         (['c.txt'], ['a.txt'], ['b.txt']))

    def test_commit_subjects(self):
        self.assertEqual(
            get_commit_subjects_since(self.base, self.ws, self.tip), ['change'])

    def test_untracked_files_are_those_the_revision_lacks(self):
        self.write_file('.gitignore', '*.log\n')
        self.write_file('new.txt')
        self.write_file('build.log')
        self.assertEqual(sorted(get_untracked_files(self.tip, self.ws)),
                         ['.gitignore', 'b.txt', 'new.txt'])
        self.assertEqual(sorted(list_tracked_files(self.ws)),
                         ['a.txt', 'b.txt'])

    def test_tracked_files_come_from_the_revision_not_the_index(self):
        self.assertEqual(sorted(list_tracked_files(self.ws, self.tip)),
                         ['a.txt', 'c.txt'])
        self.assertEqual(sorted(list_tracked_files(self.ws)),
                         ['a.txt', 'b.txt'])
        self.assertEqual(
            get_tracked_files(['b.txt', 'c.txt'], self.ws, self.tip), {'c.txt'})


class TestGetFilesAtCommits(GitRepoTestCase):
    def test_reads_every_pair_in_one_call(self):
        self.write_file('foo.txt', 'version 1')
        self.write_file('src/engine/test.cpp', 'hello')
        self.commit('first')
        first_sha = get_head_commit(self.ws)
        self.write_file('foo.txt', 'version 2\nwith\nlines\n')
        self.commit('second')

        items = [('HEAD', 'foo.txt'), (first_sha, 'foo.txt'),
                 ('HEAD', 'missing.txt'), ('HEAD', 'src'),
                 ('HEAD', 'src\\engine\\test.cpp')]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            contents = get_files_at_commits(items, self.ws)
        commands = [line for line in buffer.getvalue().splitlines()
                    if line.startswith('>')]
        self.assertEqual(len(commands), 1)
        self.assertEqual(contents, {
            ('HEAD', 'foo.txt'): b'version 2\nwith\nlines\n',
            (first_sha, 'foo.txt'): b'version 1',
            ('HEAD', 'missing.txt'): None,
            ('HEAD', 'src'): None,
            ('HEAD', 'src\\engine\\test.cpp'): b'hello',
        })

    def test_binary_content_is_returned_verbatim(self):
        path = os.path.join(self.ws, 'blob.bin')
        data = bytes(range(256)) + b'\r\n\n\0'
        with open(path, 'wb') as f:
            f.write(data)
        self.commit()
        self.assertEqual(
            get_files_at_commits([('HEAD', 'blob.bin')], self.ws),
            {('HEAD', 'blob.bin'): data})

    def test_empty_input(self):
        self.assertEqual(get_files_at_commits([], self.ws), {})


class TestGetBlobOids(GitRepoTestCase):
    def test_returns_oid_for_existing_file(self):
        self.write_file('foo.txt', 'hello world')
        self.commit()
        oids = get_blob_oids([('HEAD', 'foo.txt')], self.ws)
        expected = self.git('rev-parse', 'HEAD:foo.txt')
        self.assertEqual(oids, {('HEAD', 'foo.txt'): expected})

    def test_returns_none_for_missing_file(self):
        self.write_file('foo.txt', 'hello')
        self.commit()
        oids = get_blob_oids([('HEAD', 'nonexistent.txt')], self.ws)
        self.assertEqual(oids, {('HEAD', 'nonexistent.txt'): None})

    def test_resolves_all_pairs_in_one_call(self):
        """Mixed commits, equal and changed content and missing files resolve in one call."""
        first_sha, _ = self.commit_history(
            ('first', {'foo.txt': 'version 1', 'copy.txt': 'version 1'}),
            ('second', {'foo.txt': 'version 2'}))
        oids = get_blob_oids([
            (first_sha, 'foo.txt'),
            (first_sha, 'copy.txt'),
            ('HEAD', 'foo.txt'),
            ('HEAD', 'missing.txt'),
        ], self.ws)

        self.assertEqual(oids[(first_sha, 'foo.txt')],
                         oids[(first_sha, 'copy.txt')])
        self.assertNotEqual(oids[(first_sha, 'foo.txt')],
                            oids[('HEAD', 'foo.txt')])
        self.assertIsNone(oids[('HEAD', 'missing.txt')])

    def test_backslash_paths_normalized(self):
        self.write_file('src/engine/test.cpp', 'hello')
        self.commit()
        oids = get_blob_oids([('HEAD', 'src\\engine\\test.cpp'),
                              ('HEAD', 'src/engine/test.cpp')], self.ws)
        self.assertIsNotNone(oids[('HEAD', 'src\\engine\\test.cpp')])
        self.assertEqual(oids[('HEAD', 'src\\engine\\test.cpp')],
                         oids[('HEAD', 'src/engine/test.cpp')])

    def test_empty_input(self):
        self.assertEqual(get_blob_oids([], self.ws), {})


class TestGetHeadCommit(GitRepoTestCase):
    def test_returns_sha(self):
        self.write_file('foo.txt', 'hello')
        self.commit()
        sha = get_head_commit(self.ws)
        self.assertEqual(len(sha), 40)
        self.assertTrue(all(c in '0123456789abcdef' for c in sha))


SYNC_100 = 'git-p4son: p4 sync //ws/...@100'
SYNC_200 = 'git-p4son: p4 sync //ws/...@200'


class TestFindBaseCommits(GitRepoTestCase):
    def test_returns_most_recent_sync_commit_touching_file(self):
        _, s1 = self.commit_history((SYNC_100, {'a.cpp': 'X'}),
                                    (SYNC_200, {'a.cpp': 'X2'}))
        self.assertEqual(find_base_commits(['a.cpp'], 'HEAD', self.ws),
                         {'a.cpp': s1})

    def test_skips_sync_commits_that_did_not_touch_file(self):
        s0, _ = self.commit_history((SYNC_100, {'a.cpp': 'X'}),
                                    (SYNC_200, {'b.cpp': 'B'}))
        self.assertEqual(find_base_commits(['a.cpp'], 'HEAD', self.ws),
                         {'a.cpp': s0})

    def test_skips_user_commits_that_touched_file(self):
        s0, _ = self.commit_history((SYNC_100, {'a.cpp': 'X'}),
                                    ('user: my local edit', {'a.cpp': 'Y'}))
        self.assertEqual(find_base_commits(['a.cpp'], 'HEAD', self.ws),
                         {'a.cpp': s0})

    def test_falls_back_to_introducing_commit(self):
        """Files never touched by a sync commit fall back to the commit that added them."""
        s0, _ = self.commit_history(('initial bulk import', {'a.cpp': 'X'}),
                                    ('user: modify a.cpp', {'a.cpp': 'Y'}))
        self.assertEqual(find_base_commits(['a.cpp'], 'HEAD', self.ws),
                         {'a.cpp': s0})

    def test_fallback_is_most_recent_add_when_readded(self):
        """A deleted and re-added file falls back to its most recent add."""
        *_, s_readd = self.commit_history(('add a.cpp', {'a.cpp': 'X'}),
                                          ('delete a.cpp', {'a.cpp': None}),
                                          ('readd a.cpp', {'a.cpp': 'Z'}))
        self.assertEqual(find_base_commits(['a.cpp'], 'HEAD', self.ws),
                         {'a.cpp': s_readd})

    def test_sync_commit_wins_over_more_recent_add(self):
        """A sync commit touching the file wins over a later re-add by a user commit."""
        s0, *_ = self.commit_history((SYNC_100, {'a.cpp': 'X'}),
                                     ('user: delete a.cpp', {'a.cpp': None}),
                                     ('user: readd a.cpp', {'a.cpp': 'Z'}))
        self.assertEqual(find_base_commits(['a.cpp'], 'HEAD', self.ws),
                         {'a.cpp': s0})

    def test_returns_none_when_file_not_in_history(self):
        self.commit_history(('add b.cpp', {'b.cpp': 'B'}))
        self.assertEqual(find_base_commits(['a.cpp'], 'HEAD', self.ws),
                         {'a.cpp': None})

    def test_respects_before_commit_bound(self):
        """Looking from an earlier commit does not see the later sync commit."""
        s0, _ = self.commit_history((SYNC_100, {'a.cpp': 'X'}),
                                    (SYNC_200, {'a.cpp': 'X2'}))
        self.assertEqual(find_base_commits(['a.cpp'], s0, self.ws),
                         {'a.cpp': s0})

    def test_backslash_paths_keyed_by_input(self):
        s0, = self.commit_history((SYNC_100, {'src/engine/test.cpp': 'X'}))
        self.assertEqual(
            find_base_commits(['src\\engine\\test.cpp'], 'HEAD', self.ws),
            {'src\\engine\\test.cpp': s0})

    def test_non_ascii_paths_match(self):
        """Paths git would C-quote in --name-status output still match."""
        s0, = self.commit_history((SYNC_100, {'bäck.cpp': 'X'}))
        self.assertEqual(find_base_commits(['bäck.cpp'], 'HEAD', self.ws),
                         {'bäck.cpp': s0})

    def test_paths_git_would_c_quote_match(self):
        """Quotes and tabs come back verbatim, not as "we\\"ird.cpp"."""
        names = ['we"ird.cpp', 'tab\tname.cpp']
        s0, _ = self.commit_history(
            (SYNC_100, dict.fromkeys(names, 'X')),
            ('local change', dict.fromkeys(names, 'local')))
        self.assertEqual(find_base_commits(names, 'HEAD', self.ws),
                         dict.fromkeys(names, s0))

    @mock.patch('git_p4son.git.run')
    def test_commit_without_file_entries_is_skipped(self, mock_run):
        """A header with no status after it, e.g. a merge, does not swallow the next commit's entries."""
        mock_run.return_value = make_run_result(
            stdout=b'\x01m1\x01Merge branch\0\x01s1\x01git-p4son: p4 sync //ws/...@100\nM\0a.cpp\0')
        result = find_base_commits(['a.cpp'], 'HEAD', self.ws)
        self.assertEqual(result, {'a.cpp': 's1'})

    def test_multiple_files_resolved_in_one_walk(self):
        s_sync, s_add = self.commit_history((SYNC_100, {'a.cpp': 'X'}),
                                            ('user: add b.cpp', {'b.cpp': 'B'}))
        result = find_base_commits(
            ['a.cpp', 'b.cpp', 'missing.cpp'], 'HEAD', self.ws)
        self.assertEqual(result, {'a.cpp': s_sync,
                                  'b.cpp': s_add,
                                  'missing.cpp': None})

    def test_one_walk_for_any_number_of_paths(self):
        """All paths go on stdin, so a single git log call resolves them all."""
        s_sync, s_add = self.commit_history((SYNC_100, {'a.cpp': 'X'}),
                                            ('user: add b.cpp', {'b.cpp': 'B'}))
        missing = [f'missing/file{i}.cpp' for i in range(5000)]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = find_base_commits(
                ['a.cpp', 'b.cpp'] + missing, 'HEAD', self.ws)
        commands = [line for line in buffer.getvalue().splitlines()
                    if line.startswith('>')]
        self.assertEqual(len(commands), 1)
        self.assertIn('--stdin', commands[0])
        self.assertEqual(result['a.cpp'], s_sync)
        self.assertEqual(result['b.cpp'], s_add)
        self.assertEqual({result[f] for f in missing}, {None})

    def test_only_git_p4son_sync_subjects_are_baselines(self):
        """A commit whose subject merely looks like a sync is not a baseline."""
        s_sync, _ = self.commit_history(
            (SYNC_100, {'a.cpp': 'X'}),
            ('other: p4 sync //ws/...@200', {'a.cpp': 'Y'}))
        self.assertEqual(find_base_commits(['a.cpp'], 'HEAD', self.ws),
                         {'a.cpp': s_sync})

    def test_empty_input(self):
        self.assertEqual(find_base_commits([], 'HEAD', self.ws), {})


class TestGitLastSyncedChangelist(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_extracts_changelist(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'git-p4son: p4 sync //...@12345'
        ])
        self.assertEqual(git_last_synced_changelist('/ws'), 12345)

    @mock.patch('git_p4son.git.run')
    def test_extracts_changelist_fail(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'other: p4 sync //...@12345'
        ])
        self.assertIsNone(git_last_synced_changelist('/ws'))

    @mock.patch('git_p4son.git.run')
    def test_no_match(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '"some other commit message"'
        ])
        self.assertIsNone(git_last_synced_changelist('/ws'))

    @mock.patch('git_p4son.git.run')
    def test_empty_output(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        self.assertIsNone(git_last_synced_changelist('/ws'))

    @mock.patch('git_p4son.git.run')
    def test_command_failure(self, mock_run):
        mock_run.side_effect = RunError('git log failed')
        with self.assertRaises(RunError):
            git_last_synced_changelist('/ws')

    @mock.patch('git_p4son.git.run')
    def test_new_format_with_depot_root(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'git-p4son: p4 sync //my-client/Engine/Source/...@12345'
        ])
        self.assertEqual(git_last_synced_changelist('/ws'), 12345)

    @mock.patch('git_p4son.git.run')
    def test_uses_git_grep_to_search_history(self, mock_run):
        """git log --grep finds sync commits even when HEAD is not one."""
        mock_run.return_value = make_run_result(stdout=[
            'git-p4son: p4 sync //...@99999'
        ])
        self.assertEqual(git_last_synced_changelist('/ws'), 99999)
        cmd = mock_run.call_args[0][0]
        self.assertIn('--grep=^git-p4son: p4 sync ', cmd)


class TestGitLastSyncedChangelistInRepo(GitRepoTestCase):
    def test_skips_commits_that_only_mention_a_sync_subject(self):
        self.write_file('a.cpp', 'X')
        self.commit('git-p4son: p4 sync //ws/...@100')
        self.write_file('a.cpp', 'Y')
        self.commit('Revert "git-p4son: p4 sync //ws/...@200"')

        self.assertEqual(git_last_synced_changelist(self.ws), 100)


class TestSyncSubject(unittest.TestCase):
    def test_round_trip(self):
        subject = format_sync_subject('//ws/Engine', 12345)
        self.assertEqual(subject, 'git-p4son: p4 sync //ws/Engine/...@12345')
        self.assertEqual(parse_sync_subject(subject), 12345)

    def test_other_subjects_are_not_sync_subjects(self):
        for subject in ['other: p4 sync //ws/...@1', 'Fix p4 sync //ws/...@1',
                        'git-p4son: p4 sync //ws/...@head', 'git-p4son: p4 sync //ws/...@1 extra']:
            with self.subTest(subject=subject):
                self.assertIsNone(parse_sync_subject(subject))


class TestMergeFile(unittest.TestCase):
    def _write_inputs(self, tmpdir, current, base, other):
        paths = {}
        for name, content in [('current', current), ('base', base),
                              ('other', other)]:
            path = os.path.join(tmpdir, name)
            with open(path, 'wb') as f:
                f.write(content)
            paths[name] = path
        return paths

    def test_clean_merge(self):
        base = b'aaa\nbbb\nccc\nddd\neee\nfff\nggg\n'
        current = b'aaa\nbbb changed by p4\nccc\nddd\neee\nfff\nggg\n'
        other = b'aaa\nbbb\nccc\nddd\neee\nfff changed locally\nggg\n'

        with tempfile.TemporaryDirectory() as tmpdir:
            p = self._write_inputs(tmpdir, current, base, other)
            clean, merged = merge_file(p['current'], p['base'], p['other'],
                                       ('Perforce', 'base', 'local'))
            self.assertTrue(clean)
            self.assertIn(b'changed by p4', merged)
            self.assertIn(b'changed locally', merged)

    def test_conflict(self):
        base = b'line1\noriginal\nline3\n'
        current = b'line1\np4 version\nline3\n'
        other = b'line1\nlocal version\nline3\n'

        with tempfile.TemporaryDirectory() as tmpdir:
            p = self._write_inputs(tmpdir, current, base, other)
            clean, merged = merge_file(p['current'], p['base'], p['other'],
                                       ('Perforce', 'base', 'local'))
            self.assertFalse(clean)
            self.assertIn(b'<<<<<<< Perforce\n', merged)
            self.assertIn(b'>>>>>>> local\n', merged)


class TestGetCurrentBranch(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_returns_branch_name(self, mock_run):
        mock_run.return_value = RunResult(0, ['feat/foo'], [])
        result = get_current_branch('/ws')
        mock_run.assert_called_once_with(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd='/ws',
        )
        self.assertEqual(result, 'feat/foo')

    @mock.patch('git_p4son.git._get_rebase_branch', return_value=None)
    @mock.patch('git_p4son.git.run')
    def test_detached_head_returns_none(self, mock_run, mock_rebase):
        mock_run.return_value = RunResult(0, ['HEAD'], [])
        result = get_current_branch('/ws')
        self.assertIsNone(result)
        mock_rebase.assert_called_once_with('/ws')

    @mock.patch('git_p4son.git._get_rebase_branch', return_value='feat/my-branch')
    @mock.patch('git_p4son.git.run')
    def test_detached_head_during_rebase_returns_branch(self, mock_run, mock_rebase):
        mock_run.return_value = RunResult(0, ['HEAD'], [])
        result = get_current_branch('/ws')
        self.assertEqual(result, 'feat/my-branch')

    @mock.patch('git_p4son.git.run', side_effect=RunError('failed', returncode=128))
    def test_command_failure_returns_none(self, mock_run):
        result = get_current_branch('/ws')
        self.assertIsNone(result)

    @mock.patch('git_p4son.git.run', side_effect=OSError('git not found'))
    def test_missing_git_returns_none(self, mock_run):
        result = get_current_branch('/ws')
        self.assertIsNone(result)


class TestGetHeadSubject(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_returns_subject(self, mock_run):
        mock_run.return_value = RunResult(0, ['Fix bug'], [])
        result = get_head_subject('/ws')
        mock_run.assert_called_once_with(
            ['git', 'log', '-1', '--format=%s', 'HEAD'],
            cwd='/ws',
        )
        self.assertEqual(result, 'Fix bug')

    @mock.patch('git_p4son.git.run', side_effect=RunError('failed', returncode=128))
    def test_command_failure_returns_none(self, mock_run):
        result = get_head_subject('/ws')
        self.assertIsNone(result)

    @mock.patch('git_p4son.git.run')
    def test_empty_output_returns_none(self, mock_run):
        mock_run.return_value = RunResult(0, [], [])
        result = get_head_subject('/ws')
        self.assertIsNone(result)

    @mock.patch('git_p4son.git.run', side_effect=OSError('git not found'))
    def test_missing_git_returns_none(self, mock_run):
        result = get_head_subject('/ws')
        self.assertIsNone(result)


class TestGetRebaseBranch(unittest.TestCase):
    _GIT_PATHS = ['.git/rebase-merge/head-name', '.git/rebase-apply/head-name']

    def _read(self, state_dir, ref):
        """Write ref as the head-name in state_dir and read it back."""
        with tempfile.TemporaryDirectory() as tmpdir:
            if state_dir:
                os.makedirs(os.path.join(tmpdir, '.git', state_dir))
                with open(os.path.join(tmpdir, '.git', state_dir,
                                       'head-name'), 'w') as f:
                    f.write(ref + '\n')
            with mock.patch('git_p4son.git.run',
                            return_value=RunResult(0, self._GIT_PATHS, [])):
                return _get_rebase_branch(tmpdir)

    def test_reads_branch_from_rebase_merge_state(self):
        self.assertEqual(self._read('rebase-merge', 'refs/heads/feat/x'),
                         'feat/x')

    def test_reads_branch_from_rebase_apply_state(self):
        self.assertEqual(self._read('rebase-apply', 'refs/heads/feat/x'),
                         'feat/x')

    def test_keeps_a_ref_outside_refs_heads(self):
        self.assertEqual(self._read('rebase-merge', 'refs/other/x'),
                         'refs/other/x')

    def test_returns_none_when_no_rebase_state(self):
        self.assertIsNone(self._read(None, ''))

    def test_returns_none_when_rev_parse_fails(self):
        with mock.patch('git_p4son.git.run',
                        side_effect=RunError('failed', returncode=128)):
            self.assertIsNone(_get_rebase_branch('/ws'))


class TestGetCurrentBranchDuringRebase(GitRepoTestCase):
    """Both rebase backends detach HEAD; the branch being rebased is still found."""

    def _stop_mid_rebase(self, *rebase_args):
        self.write_file('f.txt', 'base')
        self.commit('base')
        self.git('switch', '-c', 'feat/x')
        self.write_file('f.txt', 'feature')
        self.commit('feature')
        self.git('switch', 'main')
        self.write_file('f.txt', 'main')
        self.commit('main')
        self.git('switch', 'feat/x')
        # Both commits change f.txt, so the rebase stops on a conflict with HEAD detached.
        self.git('rebase', *rebase_args, 'main', check=False)

    def test_merge_backend(self):
        self._stop_mid_rebase('--merge')
        self.assertEqual(get_current_branch(self.ws), 'feat/x')

    def test_apply_backend(self):
        self._stop_mid_rebase('--apply')
        self.assertEqual(get_current_branch(self.ws), 'feat/x')


class TestIsWorkspaceDir(unittest.TestCase):
    def test_returns_true_when_git_dir_exists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, '.git'))
            self.assertTrue(is_workspace_dir(tmpdir))

    def test_returns_false_when_no_git_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertFalse(is_workspace_dir(tmpdir))

    def test_returns_true_when_git_is_a_file(self):
        """In linked worktrees and submodules .git is a file pointing at the real git dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            with open(os.path.join(tmpdir, '.git'), 'w') as f:
                f.write('gitdir: /repo/.git/worktrees/feature\n')
            self.assertTrue(is_workspace_dir(tmpdir))


class TestGetWorkspaceDir(unittest.TestCase):
    def test_finds_workspace_from_subdirectory(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, '.git'))
            subdir = os.path.join(tmpdir, 'a', 'b')
            os.makedirs(subdir)
            with mock.patch('os.getcwd', return_value=subdir):
                result = get_workspace_dir()
            self.assertEqual(result, tmpdir)

    def test_returns_none_when_no_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch('os.getcwd', return_value=tmpdir):
                result = get_workspace_dir()
            self.assertIsNone(result)

    def test_stops_at_worktree_root_inside_another_repo(self):
        """Detection stops at a linked worktree root instead of an enclosing unrelated repo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            os.makedirs(os.path.join(tmpdir, '.git'))
            worktree = os.path.join(tmpdir, 'wt')
            os.makedirs(worktree)
            with open(os.path.join(worktree, '.git'), 'w') as f:
                f.write('gitdir: /repo/.git/worktrees/wt\n')
            subdir = os.path.join(worktree, 'src')
            os.makedirs(subdir)

            with mock.patch('os.getcwd', return_value=subdir):
                result = get_workspace_dir()
            self.assertEqual(result, worktree)


class TestGetDirtyFiles(unittest.TestCase):
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


class TestAddAllFiles(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_success(self, mock_run):
        mock_run.return_value = make_run_result()
        add_all_files('/ws')

    @mock.patch('git_p4son.git.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = RunError('git add failed')
        with self.assertRaises(RunError):
            add_all_files('/ws')


class TestCommit(unittest.TestCase):
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


class TestGetCommitLinesSince(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_returns_lines(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'abc1234 First commit',
            'def5678 Second commit',
        ])
        lines = get_commit_lines_since('main', '/workspace')
        self.assertEqual(
            lines, ['abc1234 First commit', 'def5678 Second commit'])
        mock_run.assert_called_once_with(
            ['git', 'log', '--format=%h %s', '--no-decorate',
             '--reverse', '--no-merges', 'main..HEAD'],
            cwd='/workspace',
        )

    @mock.patch('git_p4son.git.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = RunError('git log failed')
        with self.assertRaises(RunError):
            get_commit_lines_since('main', '/workspace')


class TestGetCommitsSince(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_returns_full_shas_and_subjects(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=['a' * 40 + ' First commit', 'b' * 40])
        self.assertEqual(get_commits_since('main', '/workspace'),
                         [('a' * 40, 'First commit'), ('b' * 40, '')])
        mock_run.assert_called_once_with(
            ['git', 'log', '--format=%H %s', '--no-decorate',
             '--reverse', '--no-merges', 'main..HEAD'],
            cwd='/workspace')

    @mock.patch('git_p4son.git.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = RunError('git log failed')
        with self.assertRaises(RunError):
            get_commits_since('main', '/workspace')


class TestGetCommitSubjectsSince(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_extracts_subjects(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            'a' * 40 + ' First commit',
            'b' * 40 + ' Second commit',
        ])
        subjects = get_commit_subjects_since('HEAD~1', '/workspace')
        self.assertEqual(subjects, ['First commit', 'Second commit'])
        mock_run.assert_called_once_with(
            ['git', 'log', '--format=%H %s', '--no-decorate',
             '--reverse', '--no-merges', 'HEAD~1..HEAD'],
            cwd='/workspace',
        )

    @mock.patch('git_p4son.git.run')
    def test_empty_log(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        subjects = get_commit_subjects_since('main', '/workspace')
        self.assertEqual(subjects, [])

    @mock.patch('git_p4son.git.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = RunError('git log failed')
        with self.assertRaises(RunError):
            get_commit_subjects_since('main', '/workspace')

    @mock.patch('git_p4son.git.run')
    def test_hash_only_line_fallback(self, mock_run):
        """A commit with an empty subject gets its short SHA."""
        mock_run.return_value = make_run_result(stdout=['abc1234' + '0' * 33])
        subjects = get_commit_subjects_since('main', '/workspace')
        self.assertEqual(subjects, ['abc1234'])


class TestGetLocalChanges(unittest.TestCase):
    @mock.patch('git_p4son.git.run')
    def test_parses_all_change_types(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=b'M\0modified.txt\0A\0added.txt\0D\0deleted.txt\0R100\0old_name.txt\0new_name.txt\0')
        changes = get_local_changes('main', '/ws')
        self.assertEqual(changes.mods, ['modified.txt'])
        self.assertEqual(changes.adds, ['added.txt'])
        self.assertEqual(changes.dels, ['deleted.txt'])
        self.assertEqual(changes.moves, [('old_name.txt', 'new_name.txt')])
        self.assertEqual(mock_run.call_args.args[0],
                         ['git', 'diff', '--name-status', '-z', 'main...HEAD'])

    @mock.patch('git_p4son.git.run')
    def test_typechange_treated_as_modify(self, mock_run):
        """T (e.g. symlink to regular file) is a content change."""
        mock_run.return_value = make_run_result(stdout=b'T\0link.txt\0')
        changes = get_local_changes('main', '/ws')
        self.assertEqual(changes.mods, ['link.txt'])

    @mock.patch('git_p4son.git.run')
    def test_copy_treated_as_add_of_destination(self, mock_run):
        """A copy is an add of the destination; the source is untouched."""
        mock_run.return_value = make_run_result(
            stdout=b'C100\0src.txt\0copy.txt\0')
        changes = get_local_changes('main', '/ws')
        self.assertEqual(changes.adds, ['copy.txt'])
        self.assertEqual(changes.mods, [])
        self.assertEqual(changes.dels, [])

    @mock.patch('git_p4son.git.run')
    def test_diff_failure(self, mock_run):
        mock_run.side_effect = RunError('diff failed')
        with self.assertRaises(RunError):
            get_local_changes('main', '/ws')

    @mock.patch('git_p4son.git.run')
    def test_unknown_status(self, mock_run):
        mock_run.return_value = make_run_result(stdout=b'X\0unknown.txt\0')
        with self.assertRaises(CommandError):
            get_local_changes('main', '/ws')


class TestGetLocalChangesInRepo(GitRepoTestCase):
    def test_changes_on_the_base_branch_since_the_fork_are_left_out(self):
        self.commit('base')
        self.git('switch', '-c', 'feat')
        self.write_file('feature.txt', 'feature.txt')
        self.commit('feature')
        self.git('switch', 'main')
        self.write_file('upstream.txt', 'upstream.txt')
        self.commit('upstream')
        self.git('switch', 'feat')

        changes = get_local_changes('main', self.ws)

        self.assertEqual(changes.adds, ['feature.txt'])
        self.assertEqual(changes.dels, [])

    def test_non_ascii_filename_returned_verbatim(self):
        """Non-ASCII filenames are returned verbatim, not C-quoted."""
        self.commit('base')
        self.write_file('bäck.txt', 'content')
        self.commit('add file')

        changes = get_local_changes('HEAD~1', self.ws)

        self.assertEqual(changes.adds, ['bäck.txt'])

    def test_special_characters_returned_verbatim(self):
        """Quotes, backslashes and tabs, which git otherwise C-quotes, come back as the real names."""
        names = ['we"ird.txt', 'back\\slash.txt', 'tab\tname.txt']
        self.write_file('old.txt', 'content to rename\n' * 10)
        self.commit('base')
        for name in names:
            self.write_file(name, name)
        self.git('mv', 'old.txt', 'new "name".txt')
        self.commit('add files')

        changes = get_local_changes('HEAD~1', self.ws)

        self.assertEqual(sorted(changes.adds), sorted(names))
        self.assertEqual(changes.moves, [('old.txt', 'new "name".txt')])


if __name__ == '__main__':
    unittest.main()
