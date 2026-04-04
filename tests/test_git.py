"""Tests for new git helper functions."""

import os
import subprocess
import tempfile
import unittest

from git_p4son.git import (
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


class TestMergeFile(unittest.TestCase):
    def test_clean_merge(self):
        base = b'aaa\nbbb\nccc\nddd\neee\nfff\nggg\n'
        current = b'aaa\nbbb changed by p4\nccc\nddd\neee\nfff\nggg\n'
        other = b'aaa\nbbb\nccc\nddd\neee\nfff changed locally\nggg\n'

        clean, merged = merge_file(current, base, other, 'test.txt')
        self.assertTrue(clean)
        self.assertIn(b'changed by p4', merged)
        self.assertIn(b'changed locally', merged)

    def test_conflict(self):
        base = b'line1\noriginal\nline3\n'
        current = b'line1\np4 version\nline3\n'
        other = b'line1\nlocal version\nline3\n'

        clean, merged = merge_file(current, base, other, 'test.txt')
        self.assertFalse(clean)
        self.assertIn(b'<<<<<<<', merged)
        self.assertIn(b'>>>>>>>', merged)


if __name__ == '__main__':
    unittest.main()
