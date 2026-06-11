"""Tests for new git helper functions."""

import os
import subprocess
import tempfile
import unittest
from unittest import mock

from git_p4son.git import (
    find_base_commits,
    get_blob_oids,
    get_file_at_commit,
    get_head_commit,
    get_tracked_files,
    merge_file,
)


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
        """A tracked file that matches a .gitignore pattern (common when
        .gitignore was copied from .p4ignore) is still git's to manage."""
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
        """ls-files would C-quote non-ASCII paths without -z, which would
        never match the input paths."""
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

    def test_empty_input(self):
        result = get_tracked_files([], self.tmpdir)
        self.assertEqual(result, set())


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
        """Mixed commits, equal/changed content and missing files all
        resolve from a single invocation."""
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
        """Files never touched by a sync commit fall back to the commit that
        added them (e.g. an initial bulk import), even the root commit."""
        self._write_file('a.cpp', 'X')
        self._commit('initial bulk import')
        s0 = self._rev_parse()
        self._write_file('a.cpp', 'Y')
        self._commit('user: modify a.cpp')

        result = find_base_commits(['a.cpp'], 'HEAD', self.tmpdir)
        self.assertEqual(result, {'a.cpp': s0})

    def test_fallback_is_most_recent_add_when_readded(self):
        """If the file was deleted and re-added, the most recent add starts
        the current lineage."""
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
        """A sync commit touching the file is the baseline even when the
        file was re-added by a user commit afterwards."""
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

    def test_matches_pergit_subjects(self):
        self._write_file('a.cpp', 'X')
        self._commit('pergit: p4 sync //ws/...@100')
        s0 = self._rev_parse()

        result = find_base_commits(['a.cpp'], 'HEAD', self.tmpdir)
        self.assertEqual(result, {'a.cpp': s0})

    def test_backslash_paths_keyed_by_input(self):
        self._write_file('src/engine/test.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s0 = self._rev_parse()

        result = find_base_commits(
            ['src\\engine\\test.cpp'], 'HEAD', self.tmpdir)
        self.assertEqual(result, {'src\\engine\\test.cpp': s0})

    def test_non_ascii_paths_match(self):
        """Paths git would normally C-quote in --name-status output must
        still match (core.quotePath is disabled for the walk)."""
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

    def test_chunking_preserves_results(self):
        """A tiny pathspec budget forces one walk per file; results must be
        identical to the single-walk case."""
        self._write_file('a.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s_sync = self._rev_parse()
        self._write_file('b.cpp', 'B')
        self._commit('user: add b.cpp')
        s_add = self._rev_parse()

        with mock.patch('git_p4son.git._PATHSPEC_LENGTH_BUDGET', 1):
            result = find_base_commits(
                ['a.cpp', 'b.cpp', 'missing.cpp'], 'HEAD', self.tmpdir)
        self.assertEqual(result, {'a.cpp': s_sync,
                                  'b.cpp': s_add,
                                  'missing.cpp': None})

    def test_empty_input(self):
        self.assertEqual(find_base_commits([], 'HEAD', self.tmpdir), {})


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
