"""Tests for new git helper functions."""

import os
import subprocess
import tempfile
import unittest

from git_p4son.git import (
    get_file_at_commit,
    get_ignored_files,
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


if __name__ == '__main__':
    unittest.main()
