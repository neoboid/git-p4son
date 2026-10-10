"""Tests for new git helper functions."""

import contextlib
import io
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from git_p4son.common import RunError
from git_p4son.git import (
    find_base_commits,
    format_sync_subject,
    get_blob_oids,
    get_file_at_commit,
    get_head_commit,
    get_tracked_files,
    git_last_sync,
    list_tracked_files,
    merge_file,
    parse_sync_subject,
)
from tests.helpers import make_run_result


class GitRepoTestCase(unittest.TestCase):
    """Base class that creates a temporary git repo for each test."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        subprocess.run(['git', 'init'], cwd=self.tmpdir,
                       capture_output=True, check=True)
        subprocess.run(['git', 'config', 'user.email', 'test@test.com'],
                       cwd=self.tmpdir, capture_output=True, check=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'],
                       cwd=self.tmpdir, capture_output=True, check=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmpdir)

    def _write_file(self, name, content):
        path = os.path.join(self.tmpdir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(content)
        return path

    def _commit(self, message='test commit'):
        subprocess.run(['git', 'add', '.'], cwd=self.tmpdir,
                       capture_output=True, check=True)
        subprocess.run(['git', 'commit', '-m', message],
                       cwd=self.tmpdir, capture_output=True, check=True)


class TestGetTrackedFiles(GitRepoTestCase):
    def test_returns_tracked_subset(self):
        self._write_file('src/main.py', 'code')
        self._commit()
        self._write_file('untracked.log', '')

        result = get_tracked_files(
            ['src/main.py', 'untracked.log'], self.tmpdir)
        self.assertEqual(result, {'src/main.py'})

    def test_tracked_file_matching_ignore_pattern_is_tracked(self):
        """A tracked file matching a .gitignore pattern is still tracked."""
        self._write_file('.gitignore', '*.ini\n')
        self._write_file('config.ini', 'tracked anyway')
        subprocess.run(['git', 'add', '-f', 'config.ini', '.gitignore'],
                       cwd=self.tmpdir, capture_output=True, check=True)
        subprocess.run(['git', 'commit', '-m', 'add'],
                       cwd=self.tmpdir, capture_output=True, check=True)
        self._write_file('other.ini', 'ignored and untracked')

        result = get_tracked_files(
            ['config.ini', 'other.ini'], self.tmpdir)
        self.assertEqual(result, {'config.ini'})

    def test_absolute_paths_map_back_to_input(self):
        self._write_file('src/main.py', 'code')
        self._commit()
        abs_path = os.path.join(self.tmpdir, 'src', 'main.py')

        result = get_tracked_files([abs_path], self.tmpdir)
        self.assertEqual(result, {abs_path})

    def test_non_ascii_paths_match(self):
        """Non-ASCII paths match the input paths rather than coming back C-quoted."""
        name = 'bäck.py'
        self._write_file(name, 'code')
        self._commit()

        result = get_tracked_files([name], self.tmpdir)
        self.assertEqual(result, {name})

    def test_chunking_preserves_results(self):
        self._write_file('a.py', 'a')
        self._write_file('b.py', 'b')
        self._commit()
        self._write_file('c.log', '')

        with mock.patch('git_p4son.git._PATHSPEC_LENGTH_BUDGET', 1):
            result = get_tracked_files(
                ['a.py', 'b.py', 'c.log'], self.tmpdir)
        self.assertEqual(result, {'a.py', 'b.py'})

    def test_batches_are_logged_as_one_line(self):
        """A batch of chunked commands is logged as one line."""
        self._write_file('a.py', 'a')
        self._write_file('b.py', 'b')
        self._commit()
        buffer = io.StringIO()
        with mock.patch('git_p4son.git._PATHSPEC_LENGTH_BUDGET', 1), \
                contextlib.redirect_stdout(buffer):
            get_tracked_files(['a.py', 'b.py', 'c.log'], self.tmpdir)
        commands = [line for line in buffer.getvalue().splitlines()
                    if line.startswith('>')]
        self.assertEqual(commands,
                         ['>  git ls-files -z -- <3 paths in 3 batches>'])

    def test_single_batch_logs_the_command(self):
        self._write_file('a.py', 'a')
        self._commit()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            get_tracked_files(['a.py'], self.tmpdir)
        self.assertIn('>  git ls-files -z -- a.py', buffer.getvalue())

    def test_empty_input(self):
        result = get_tracked_files([], self.tmpdir)
        self.assertEqual(result, set())


class TestListTrackedFiles(GitRepoTestCase):
    def test_lists_tracked_files_only(self):
        self._write_file('src/main.py', 'code')
        self._write_file('.gitignore', '*.log\n')
        self._commit()
        self._write_file('untracked.txt', '')
        self._write_file('build.log', '')

        self.assertEqual(sorted(list_tracked_files(self.tmpdir)),
                         ['.gitignore', 'src/main.py'])

    def test_non_ascii_paths_are_not_quoted(self):
        self._write_file('src/Ärlig.py', 'code')
        self._commit()
        self.assertEqual(list_tracked_files(self.tmpdir), ['src/Ärlig.py'])

    def test_empty_repo(self):
        self.assertEqual(list_tracked_files(self.tmpdir), [])


class TestGetFileAtCommit(GitRepoTestCase):
    def test_returns_file_content(self):
        self._write_file('foo.txt', 'hello world')
        self._commit()
        content = get_file_at_commit('foo.txt', 'HEAD', self.tmpdir)
        self.assertEqual(content, b'hello world')

    def test_returns_none_for_missing_file(self):
        self._write_file('foo.txt', 'hello')
        self._commit()
        content = get_file_at_commit('nonexistent.txt', 'HEAD', self.tmpdir)
        self.assertIsNone(content)

    def test_backslash_paths_normalized(self):
        self._write_file('src/engine/test.cpp', 'hello')
        self._commit()
        content = get_file_at_commit(
            'src\\engine\\test.cpp', 'HEAD', self.tmpdir)
        self.assertEqual(content, b'hello')

    def test_retrieves_from_specific_commit(self):
        self._write_file('foo.txt', 'version 1')
        self._commit('first')
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=self.tmpdir,
            capture_output=True, text=True)
        first_sha = result.stdout.strip()

        self._write_file('foo.txt', 'version 2')
        self._commit('second')

        content = get_file_at_commit('foo.txt', first_sha, self.tmpdir)
        self.assertEqual(content, b'version 1')

        content = get_file_at_commit('foo.txt', 'HEAD', self.tmpdir)
        self.assertEqual(content, b'version 2')


class TestGetBlobOids(GitRepoTestCase):
    def test_returns_oid_for_existing_file(self):
        self._write_file('foo.txt', 'hello world')
        self._commit()
        oids = get_blob_oids([('HEAD', 'foo.txt')], self.tmpdir)
        expected = subprocess.run(
            ['git', 'rev-parse', 'HEAD:foo.txt'], cwd=self.tmpdir,
            capture_output=True, text=True).stdout.strip()
        self.assertEqual(oids, {('HEAD', 'foo.txt'): expected})

    def test_returns_none_for_missing_file(self):
        self._write_file('foo.txt', 'hello')
        self._commit()
        oids = get_blob_oids([('HEAD', 'nonexistent.txt')], self.tmpdir)
        self.assertEqual(oids, {('HEAD', 'nonexistent.txt'): None})

    def test_resolves_all_pairs_in_one_call(self):
        """Mixed commits, equal and changed content and missing files resolve in one call."""
        self._write_file('foo.txt', 'version 1')
        self._write_file('copy.txt', 'version 1')
        self._commit('first')
        result = subprocess.run(
            ['git', 'rev-parse', 'HEAD'], cwd=self.tmpdir,
            capture_output=True, text=True)
        first_sha = result.stdout.strip()

        self._write_file('foo.txt', 'version 2')
        self._commit('second')

        oids = get_blob_oids([
            (first_sha, 'foo.txt'),
            (first_sha, 'copy.txt'),
            ('HEAD', 'foo.txt'),
            ('HEAD', 'missing.txt'),
        ], self.tmpdir)

        self.assertEqual(oids[(first_sha, 'foo.txt')],
                         oids[(first_sha, 'copy.txt')])
        self.assertNotEqual(oids[(first_sha, 'foo.txt')],
                            oids[('HEAD', 'foo.txt')])
        self.assertIsNone(oids[('HEAD', 'missing.txt')])

    def test_backslash_paths_normalized(self):
        self._write_file('src/engine/test.cpp', 'hello')
        self._commit()
        oids = get_blob_oids([('HEAD', 'src\\engine\\test.cpp'),
                              ('HEAD', 'src/engine/test.cpp')], self.tmpdir)
        self.assertIsNotNone(oids[('HEAD', 'src\\engine\\test.cpp')])
        self.assertEqual(oids[('HEAD', 'src\\engine\\test.cpp')],
                         oids[('HEAD', 'src/engine/test.cpp')])

    def test_empty_input(self):
        self.assertEqual(get_blob_oids([], self.tmpdir), {})


class TestGetHeadCommit(GitRepoTestCase):
    def test_returns_sha(self):
        self._write_file('foo.txt', 'hello')
        self._commit()
        sha = get_head_commit(self.tmpdir)
        self.assertEqual(len(sha), 40)
        self.assertTrue(all(c in '0123456789abcdef' for c in sha))


class TestFindBaseCommits(GitRepoTestCase):
    def _rev_parse(self, ref='HEAD'):
        result = subprocess.run(
            ['git', 'rev-parse', ref], cwd=self.tmpdir,
            capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def test_returns_most_recent_sync_commit_touching_file(self):
        self._write_file('a.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s0 = self._rev_parse()
        self._write_file('a.cpp', 'X2')
        self._commit('git-p4son: p4 sync //ws/...@200')
        s1 = self._rev_parse()

        result = find_base_commits(['a.cpp'], 'HEAD', self.tmpdir)
        self.assertEqual(result, {'a.cpp': s1})
        self.assertNotEqual(s0, s1)

    def test_skips_sync_commits_that_did_not_touch_file(self):
        self._write_file('a.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s0 = self._rev_parse()
        self._write_file('b.cpp', 'B')
        self._commit('git-p4son: p4 sync //ws/...@200')

        result = find_base_commits(['a.cpp'], 'HEAD', self.tmpdir)
        self.assertEqual(result, {'a.cpp': s0})

    def test_skips_user_commits_that_touched_file(self):
        self._write_file('a.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s0 = self._rev_parse()
        self._write_file('a.cpp', 'Y')
        self._commit('user: my local edit')

        result = find_base_commits(['a.cpp'], 'HEAD', self.tmpdir)
        self.assertEqual(result, {'a.cpp': s0})

    def test_falls_back_to_introducing_commit(self):
        """Files never touched by a sync commit fall back to the commit that added them."""
        self._write_file('a.cpp', 'X')
        self._commit('initial bulk import')
        s0 = self._rev_parse()
        self._write_file('a.cpp', 'Y')
        self._commit('user: modify a.cpp')

        result = find_base_commits(['a.cpp'], 'HEAD', self.tmpdir)
        self.assertEqual(result, {'a.cpp': s0})

    def test_fallback_is_most_recent_add_when_readded(self):
        """A deleted and re-added file falls back to its most recent add."""
        self._write_file('a.cpp', 'X')
        self._commit('add a.cpp')
        os.remove(os.path.join(self.tmpdir, 'a.cpp'))
        self._commit('delete a.cpp')
        self._write_file('a.cpp', 'Z')
        self._commit('readd a.cpp')
        s_readd = self._rev_parse()

        result = find_base_commits(['a.cpp'], 'HEAD', self.tmpdir)
        self.assertEqual(result, {'a.cpp': s_readd})

    def test_sync_commit_wins_over_more_recent_add(self):
        """A sync commit touching the file wins over a later re-add by a user commit."""
        self._write_file('a.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s0 = self._rev_parse()
        os.remove(os.path.join(self.tmpdir, 'a.cpp'))
        self._commit('user: delete a.cpp')
        self._write_file('a.cpp', 'Z')
        self._commit('user: readd a.cpp')

        result = find_base_commits(['a.cpp'], 'HEAD', self.tmpdir)
        self.assertEqual(result, {'a.cpp': s0})

    def test_returns_none_when_file_not_in_history(self):
        self._write_file('b.cpp', 'B')
        self._commit('add b.cpp')

        result = find_base_commits(['a.cpp'], 'HEAD', self.tmpdir)
        self.assertEqual(result, {'a.cpp': None})

    def test_respects_before_commit_bound(self):
        self._write_file('a.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s0 = self._rev_parse()
        self._write_file('a.cpp', 'X2')
        self._commit('git-p4son: p4 sync //ws/...@200')

        # Looking from s0 should not see the later sync commit.
        result = find_base_commits(['a.cpp'], s0, self.tmpdir)
        self.assertEqual(result, {'a.cpp': s0})

    def test_backslash_paths_keyed_by_input(self):
        self._write_file('src/engine/test.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s0 = self._rev_parse()

        result = find_base_commits(
            ['src\\engine\\test.cpp'], 'HEAD', self.tmpdir)
        self.assertEqual(result, {'src\\engine\\test.cpp': s0})

    def test_non_ascii_paths_match(self):
        """Paths git would C-quote in --name-status output still match."""
        self._write_file('bäck.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s0 = self._rev_parse()

        result = find_base_commits(['bäck.cpp'], 'HEAD', self.tmpdir)
        self.assertEqual(result, {'bäck.cpp': s0})

    def test_multiple_files_resolved_in_one_walk(self):
        self._write_file('a.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s_sync = self._rev_parse()
        self._write_file('b.cpp', 'B')
        self._commit('user: add b.cpp')
        s_add = self._rev_parse()

        result = find_base_commits(
            ['a.cpp', 'b.cpp', 'missing.cpp'], 'HEAD', self.tmpdir)
        self.assertEqual(result, {'a.cpp': s_sync,
                                  'b.cpp': s_add,
                                  'missing.cpp': None})

    def test_one_walk_for_any_number_of_paths(self):
        """All paths go on stdin, so a single git log call resolves them all."""
        self._write_file('a.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s_sync = self._rev_parse()
        self._write_file('b.cpp', 'B')
        self._commit('user: add b.cpp')
        s_add = self._rev_parse()

        missing = [f'missing/file{i}.cpp' for i in range(5000)]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = find_base_commits(
                ['a.cpp', 'b.cpp'] + missing, 'HEAD', self.tmpdir)
        commands = [line for line in buffer.getvalue().splitlines()
                    if line.startswith('>')]
        self.assertEqual(len(commands), 1)
        self.assertIn('--stdin', commands[0])
        self.assertEqual(result['a.cpp'], s_sync)
        self.assertEqual(result['b.cpp'], s_add)
        self.assertEqual({result[f] for f in missing}, {None})

    def test_empty_input(self):
        self.assertEqual(find_base_commits([], 'HEAD', self.tmpdir), {})


class TestGitLastSync(unittest.TestCase):
    HASH = 'abc123def456' * 3 + 'abcd'  # 40-char fake hash

    @mock.patch('git_p4son.git.run_with_output')
    def test_extracts_changelist_and_commit(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            f'{self.HASH} git-p4son: p4 sync //...@12345'
        ])
        result = git_last_sync('/ws')
        self.assertEqual(result.changelist, 12345)
        self.assertEqual(result.commit, self.HASH)

    @mock.patch('git_p4son.git.run_with_output')
    def test_extracts_changelist_fail(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            f'{self.HASH} other: p4 sync //...@12345'
        ])
        result = git_last_sync('/ws')
        self.assertIsNone(result)

    @mock.patch('git_p4son.git.run_with_output')
    def test_no_match(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            f'{self.HASH} "some other commit message"'
        ])
        result = git_last_sync('/ws')
        self.assertIsNone(result)

    @mock.patch('git_p4son.git.run_with_output')
    def test_empty_output(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[])
        result = git_last_sync('/ws')
        self.assertIsNone(result)

    @mock.patch('git_p4son.git.run_with_output')
    def test_command_failure(self, mock_rwo):
        mock_rwo.side_effect = RunError('git log failed')
        with self.assertRaises(RunError):
            git_last_sync('/ws')

    @mock.patch('git_p4son.git.run_with_output')
    def test_new_format_with_depot_root(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            f'{self.HASH} git-p4son: p4 sync //my-client/Engine/Source/...@12345'
        ])
        result = git_last_sync('/ws')
        self.assertEqual(result.changelist, 12345)

    @mock.patch('git_p4son.git.run_with_output')
    def test_uses_git_grep_to_search_history(self, mock_rwo):
        """git log --grep finds sync commits even when HEAD is not one."""
        mock_rwo.return_value = make_run_result(stdout=[
            f'{self.HASH} git-p4son: p4 sync //...@99999'
        ])
        result = git_last_sync('/ws')
        self.assertEqual(result.changelist, 99999)
        cmd = mock_rwo.call_args[0][0]
        self.assertIn('--grep=^git-p4son: p4 sync ', cmd)


class TestGitLastSyncInRepo(GitRepoTestCase):
    def test_skips_commits_that_only_mention_a_sync_subject(self):
        self._write_file('a.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s_sync = get_head_commit(self.tmpdir)
        self._write_file('a.cpp', 'Y')
        self._commit('Revert "git-p4son: p4 sync //ws/...@200"')

        result = git_last_sync(self.tmpdir)
        self.assertEqual(result.changelist, 100)
        self.assertEqual(result.commit, s_sync)


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
            clean, merged = merge_file(p['current'], p['base'], p['other'])
            self.assertTrue(clean)
            self.assertIn(b'changed by p4', merged)
            self.assertIn(b'changed locally', merged)

    def test_conflict(self):
        base = b'line1\noriginal\nline3\n'
        current = b'line1\np4 version\nline3\n'
        other = b'line1\nlocal version\nline3\n'

        with tempfile.TemporaryDirectory() as tmpdir:
            p = self._write_inputs(tmpdir, current, base, other)
            clean, merged = merge_file(p['current'], p['base'], p['other'])
            self.assertFalse(clean)
            self.assertIn(b'<<<<<<< Perforce\n', merged)
            self.assertIn(b'>>>>>>> local\n', merged)


if __name__ == '__main__':
    unittest.main()
