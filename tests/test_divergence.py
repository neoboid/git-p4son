"""Tests for the divergence cache prototype."""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from git_p4son.divergence import ENABLE_ENV, clean_paths, load, record_sync
from git_p4son.git import find_base_commits, get_blob_oids


def _enabled():
    return mock.patch.dict(os.environ, {ENABLE_ENV: '1'})


class TestDivergenceCache(unittest.TestCase):
    def setUp(self):
        self.ws = tempfile.mkdtemp()
        self._git('init')
        self._git('config', 'user.email', 'test@test.com')
        self._git('config', 'user.name', 'Test')

    def tearDown(self):
        shutil.rmtree(self.ws)

    def _git(self, *args):
        return subprocess.run(['git'] + list(args), cwd=self.ws,
                              capture_output=True, text=True,
                              check=True).stdout.strip()

    def _commit(self, subject, **files):
        for name, content in files.items():
            with open(os.path.join(self.ws, name), 'w') as f:
                f.write(content + '\n')
        self._git('add', '-A')
        self._git('commit', '-m', subject)
        return self._git('rev-parse', 'HEAD')

    def _sync(self, changelist, **files):
        """Commit as a sync commit and record it the way sync_command does."""
        pre = self._git('rev-parse', 'HEAD')
        post = self._commit(f'{changelist}: p4 sync //ws/...@{changelist}',
                            **files)
        record_sync(pre, post, self.ws)
        return post

    def _classify(self, path):
        """The verdict prepare_writable_files would reach for path."""
        head = self._git('rev-parse', 'HEAD')
        if path in clean_paths(head, self.ws):
            return 'unchanged'
        base = find_base_commits([path], head, self.ws)[path]
        if base is None:
            return 'changed'
        oids = get_blob_oids([(head, path), (base, path)], self.ws)
        return ('unchanged' if oids[(head, path)] == oids[(base, path)]
                else 'changed')

    def test_disabled_by_default(self):
        self._commit('Initial import', **{'f.txt': 'v0'})
        self._sync(100, **{'f.txt': 'v1'})
        head = self._git('rev-parse', 'HEAD')
        self.assertEqual(clean_paths(head, self.ws), set())
        self.assertIsNone(load(self.ws))

    def test_sync_commit_marks_paths_clean(self):
        with _enabled():
            self._commit('Initial import', **{'f.txt': 'v0', 'g.txt': 'w0'})
            self._sync(100, **{'f.txt': 'v1'})
            head = self._git('rev-parse', 'HEAD')
            # Only the file the sync commit touched is known clean; the
            # other has never been seen and stays a candidate.
            self.assertEqual(clean_paths(head, self.ws), {'f.txt'})

    def test_local_commit_drops_path_from_clean(self):
        with _enabled():
            self._commit('Initial import', **{'f.txt': 'v0'})
            self._sync(100, **{'f.txt': 'v1'})
            self._commit('Local fix', **{'f.txt': 'local'})
            head = self._git('rev-parse', 'HEAD')
            self.assertEqual(clean_paths(head, self.ws), set())

    def test_local_change_survives_sync_that_skips_the_file(self):
        """The case a diff against the newest sync commit gets wrong: a local
        commit, then a sync that does not touch the file, leaves HEAD content
        equal to the newest sync commit while the baseline is older."""
        with _enabled():
            self._commit('Initial import', **{'f.txt': 'v0', 'g.txt': 'w0'})
            self._sync(100, **{'f.txt': 'v1'})
            self._commit('Local fix', **{'f.txt': 'local'})
            self._sync(200, **{'g.txt': 'w1'})

            self.assertEqual(self._classify('f.txt'), 'changed')
            self.assertEqual(self._classify('g.txt'), 'unchanged')

    def test_cache_discarded_when_head_is_not_a_descendant(self):
        """A branch switch or rebase can strand the cached commit, and the
        verdicts recorded against it no longer apply."""
        with _enabled():
            self._commit('Initial import', **{'f.txt': 'v0'})
            self._git('checkout', '-qb', 'feature')
            self._sync(100, **{'f.txt': 'v1'})
            self.assertEqual(load(self.ws).clean, {'f.txt'})

            self._git('checkout', '-q', self._git('rev-list', '--max-parents=0',
                                                  'HEAD'))
            head = self._git('rev-parse', 'HEAD')
            self.assertEqual(clean_paths(head, self.ws), set())

    def test_cache_is_gitignored(self):
        with _enabled():
            self._commit('Initial import', **{'f.txt': 'v0'})
            self._sync(100, **{'f.txt': 'v1'})
            ignored = subprocess.run(
                ['git', 'check-ignore', '-q', '.git-p4son/divergence.cache'],
                cwd=self.ws, capture_output=True)
            self.assertEqual(ignored.returncode, 0)


if __name__ == '__main__':
    unittest.main()
