"""Tests for new git helper functions."""

import os
import subprocess
import tempfile
import unittest

from git_p4son.git import (
    find_introducing_commit_for_file,
    find_last_sync_commit_for_file,
    get_file_at_commit,
    get_head_commit,
    get_ignored_files,
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


class TestGetIgnoredFiles(GitRepoTestCase):
    def test_returns_ignored_files(self):
        self._write_file('.gitignore', '*.log\nbuild/\n')
        self._commit()
        self._write_file('build/out.o', '')
        self._write_file('app.log', '')
        self._write_file('src/main.py', '')

        result = get_ignored_files(
            ['build/out.o', 'app.log', 'src/main.py'], self.tmpdir)
        self.assertIn('build/out.o', result)
        self.assertIn('app.log', result)
        self.assertNotIn('src/main.py', result)

    def test_no_matches_returns_empty(self):
        self._write_file('.gitignore', '*.log\n')
        self._commit()
        result = get_ignored_files(['src/main.py'], self.tmpdir)
        self.assertEqual(result, set())

    def test_empty_input(self):
        result = get_ignored_files([], self.tmpdir)
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


class TestGetHeadCommit(GitRepoTestCase):
    def test_returns_sha(self):
        self._write_file('foo.txt', 'hello')
        self._commit()
        sha = get_head_commit(self.tmpdir)
        self.assertEqual(len(sha), 40)
        self.assertTrue(all(c in '0123456789abcdef' for c in sha))


class TestFindLastSyncCommitForFile(GitRepoTestCase):
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

        result = find_last_sync_commit_for_file('a.cpp', 'HEAD', self.tmpdir)
        self.assertEqual(result, s1)
        self.assertNotEqual(result, s0)

    def test_skips_sync_commits_that_did_not_touch_file(self):
        self._write_file('a.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s0 = self._rev_parse()
        self._write_file('b.cpp', 'B')
        self._commit('git-p4son: p4 sync //ws/...@200')

        result = find_last_sync_commit_for_file('a.cpp', 'HEAD', self.tmpdir)
        self.assertEqual(result, s0)

    def test_skips_user_commits_that_touched_file(self):
        self._write_file('a.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s0 = self._rev_parse()
        self._write_file('a.cpp', 'Y')
        self._commit('user: my local edit')

        result = find_last_sync_commit_for_file('a.cpp', 'HEAD', self.tmpdir)
        self.assertEqual(result, s0)

    def test_returns_none_when_no_sync_commit_touched_file(self):
        self._write_file('b.cpp', 'B')
        self._commit('git-p4son: p4 sync //ws/...@100')
        self._write_file('a.cpp', 'A')
        self._commit('user: add a.cpp')

        result = find_last_sync_commit_for_file('a.cpp', 'HEAD', self.tmpdir)
        self.assertIsNone(result)

    def test_respects_before_commit_bound(self):
        self._write_file('a.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s0 = self._rev_parse()
        self._write_file('a.cpp', 'X2')
        self._commit('git-p4son: p4 sync //ws/...@200')

        # Looking from s0 should not see the later sync commit.
        result = find_last_sync_commit_for_file('a.cpp', s0, self.tmpdir)
        self.assertEqual(result, s0)

    def test_matches_pergit_and_numeric_subjects(self):
        self._write_file('a.cpp', 'X')
        self._commit('pergit: p4 sync //ws/...@100')
        s0 = self._rev_parse()

        result = find_last_sync_commit_for_file('a.cpp', 'HEAD', self.tmpdir)
        self.assertEqual(result, s0)

    def test_backslash_paths_normalized(self):
        self._write_file('src/engine/test.cpp', 'X')
        self._commit('git-p4son: p4 sync //ws/...@100')
        s0 = self._rev_parse()

        result = find_last_sync_commit_for_file(
            'src\\engine\\test.cpp', 'HEAD', self.tmpdir)
        self.assertEqual(result, s0)


class TestFindIntroducingCommitForFile(GitRepoTestCase):
    def _rev_parse(self, ref='HEAD'):
        result = subprocess.run(
            ['git', 'rev-parse', ref], cwd=self.tmpdir,
            capture_output=True, text=True, check=True)
        return result.stdout.strip()

    def test_returns_add_commit_for_root_commit(self):
        """Files added in the initial commit are still detected as added."""
        self._write_file('a.cpp', 'X')
        self._commit('initial bulk import')
        s0 = self._rev_parse()

        result = find_introducing_commit_for_file(
            'a.cpp', 'HEAD', self.tmpdir)
        self.assertEqual(result, s0)

    def test_returns_add_commit_not_modification(self):
        self._write_file('a.cpp', 'X')
        self._commit('add a.cpp')
        s0 = self._rev_parse()
        self._write_file('a.cpp', 'Y')
        self._commit('modify a.cpp')

        result = find_introducing_commit_for_file(
            'a.cpp', 'HEAD', self.tmpdir)
        self.assertEqual(result, s0)

    def test_returns_most_recent_add_when_readded(self):
        """If the file was deleted and re-added, the most recent add starts
        the current lineage."""
        self._write_file('a.cpp', 'X')
        self._commit('add a.cpp')
        os.remove(os.path.join(self.tmpdir, 'a.cpp'))
        self._commit('delete a.cpp')
        self._write_file('a.cpp', 'Z')
        self._commit('readd a.cpp')
        s_readd = self._rev_parse()

        result = find_introducing_commit_for_file(
            'a.cpp', 'HEAD', self.tmpdir)
        self.assertEqual(result, s_readd)

    def test_returns_none_when_file_not_in_history(self):
        self._write_file('b.cpp', 'B')
        self._commit('add b.cpp')

        result = find_introducing_commit_for_file(
            'a.cpp', 'HEAD', self.tmpdir)
        self.assertIsNone(result)

    def test_respects_before_commit_bound(self):
        self._write_file('a.cpp', 'X')
        self._commit('add a.cpp')
        s0 = self._rev_parse()
        self._write_file('b.cpp', 'B')
        self._commit('add b.cpp')

        # Looking from s0 should not see commits after it.
        result = find_introducing_commit_for_file(
            'b.cpp', s0, self.tmpdir)
        self.assertIsNone(result)

    def test_backslash_paths_normalized(self):
        self._write_file('src/engine/test.cpp', 'X')
        self._commit('add')
        s0 = self._rev_parse()

        result = find_introducing_commit_for_file(
            'src\\engine\\test.cpp', 'HEAD', self.tmpdir)
        self.assertEqual(result, s0)


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
            self.assertIn(b'<<<<<<<', merged)
            self.assertIn(b'>>>>>>>', merged)


if __name__ == '__main__':
    unittest.main()
