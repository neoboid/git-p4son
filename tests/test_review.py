"""Tests for git_p4son.review module."""

import os
import unittest
from unittest import mock

from git_p4son.changelist_store import save_changelist_alias
from git_p4son.rebase_todo import TODO_FILE
from git_p4son.review import review_command, _step_commands
from tests.helpers import fake_rebase, GitRepoTestCase


class TestStepCommands(unittest.TestCase):
    def test_no_commit_list(self):
        self.assertEqual(_step_commands('feat', False, True),
                         ('new feat --review --no-commit-list',
                          'update feat --shelve --no-commit-list'))

    def test_alias_with_special_chars(self):
        new_cmd, update_cmd = _step_commands('my feature', False, False)
        self.assertIn("'my feature'", new_cmd)
        self.assertIn("'my feature'", update_cmd)


class TestReview(GitRepoTestCase):
    """review of the two commits on branch feature."""

    def setUp(self):
        super().setUp()
        self.commit('Base')
        self.git('switch', '-q', '-c', 'feature')
        self.commit('First commit')
        self.commit('Second commit')
        self.first, self.second = self.git(
            'log', '--reverse', '--format=%h %s', 'main..').splitlines()
        self.rebases = fake_rebase(self)

    def review(self, *argv):
        """Run review of my-feature with argv; returns everything it printed."""
        return self.run_command(review_command, [
            'review', 'my-feature', '-m', 'My feature', '-b', 'main', *argv])

    def review_fails(self, *argv, message):
        self.command_fails(review_command, [
            'review', 'my-feature', '-m', 'My feature', '-b', 'main', *argv],
            message)

    def test_rebases_with_new_then_update(self):
        self.review()
        self.assertEqual(self.rebases, [(
            ['git', 'rebase', '-i', 'main'], 'git-p4son _sequence-editor',
            f'pick {self.first}\n'
            'exec git p4son new my-feature --review '
            '-F .git-p4son/reviews/message --sleep 5\n'
            f'pick {self.second}\n'
            'exec git p4son update my-feature --shelve\n',
            'My feature')])
        self.assertFalse(os.path.exists(os.path.join(self.ws, TODO_FILE)))

    def test_force(self):
        self.review('--force')
        self.assertIn('exec git p4son new my-feature --review --force',
                      self.rebases[0][2])

    def test_no_edit_todo(self):
        """Accepts the todo without an editor, so none needs configuring."""
        with mock.patch('git_p4son.rebase_todo.resolve_editor') as editor:
            self.review('--no-edit-todo')
        editor.assert_not_called()
        self.assertEqual(self.rebases[0][1],
                         'git-p4son _sequence-editor --no-edit')

    def test_dry_run_prints_the_todo(self):
        out = self.review('--dry-run', '-m', 'Line one\nLine two')
        self.assertEqual(self.rebases, [])
        self.assertIn('exec git p4son update my-feature --shelve', out)
        self.assertIn('Line one\nLine two', out)

    def test_no_commits(self):
        self.review_fails('-b', 'feature', message='No commits found')
        self.assertEqual(self.rebases, [])

    def test_no_editor_fails_before_the_rebase(self):
        with mock.patch('git_p4son.rebase_todo.resolve_editor',
                        return_value=None):
            self.review_fails(message='No git editor configured')
        self.assertEqual(self.rebases, [])

    def test_existing_alias_without_force(self):
        save_changelist_alias('my-feature', '1', self.ws, force=False)
        self.review_fails(message='already exists')
        self.assertEqual(self.rebases, [])

    def test_dirty_workspace(self):
        self.write_file('stray.txt')
        self.review_fails(message='not clean')
        self.assertEqual(self.rebases, [])

    def test_rebase_failure(self):
        self.rebases = fake_rebase(self, returncode=1)
        self.review_fails(message='Rebase did not complete')
        self.assertFalse(os.path.exists(os.path.join(self.ws, TODO_FILE)))


if __name__ == '__main__':
    unittest.main()
