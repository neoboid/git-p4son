"""Tests for git_p4son.jj and git_p4son.jj_lock modules."""

import argparse
import os
import shutil
import subprocess
import unittest
from unittest import mock

from git_p4son import jj
from git_p4son.common import CommandError
from git_p4son.jj_lock import working_copy_lock
from git_p4son.changelist_store import load_changelist_alias
from git_p4son.cli import create_parser, run_command
from tests.helpers import captured_output, P4RepoTestCase, WorkspaceTestCase


def isolate_jj(test):
    """Give jj a fixed user and no user config for the rest of the test."""
    config = os.path.join(
        test.ws, '..', f'{os.path.basename(test.ws)}-jj.toml')
    open(config, 'w').close()
    test.addCleanup(os.remove, config)
    patcher = mock.patch.dict(os.environ, {'JJ_USER': 'Test', 'JJ_EMAIL': 'test@example.com',
                                           'JJ_CONFIG': config})
    patcher.start()
    test.addCleanup(patcher.stop)


class TestWorkingCopyLock(WorkspaceTestCase):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.ws, '.jj', 'working_copy'))

    def test_busy_lock_raises(self):
        with working_copy_lock(self.ws):
            with self.assertRaisesRegex(CommandError, 'busy'):
                with working_copy_lock(self.ws):
                    pass

    def test_lock_is_released(self):
        with working_copy_lock(self.ws):
            pass
        with working_copy_lock(self.ws):
            pass


class TestOperationOutsideJj(WorkspaceTestCase):
    def test_yields_none_outside_a_jj_workspace(self):
        with jj.operation(self.ws) as workspace:
            self.assertIsNone(workspace)

    def test_change_revision_without_a_workspace(self):
        self.assertIsNone(jj.change_revision(
            argparse.Namespace(jj_workspace=None)))


@unittest.skipUnless(shutil.which('jj'), 'jj is not installed')
class JjRepoTestCase(WorkspaceTestCase):
    """A colocated jj repo: main at "base", feat at "feature" on top, and an empty working change above feat."""

    def build_workspace(self):
        isolate_jj(self)
        for command in (['git', 'init', '--colocate'],
                        ['describe', '-m', 'base'], ['bookmark',
                                                     'create', 'main', '-r', '@'],
                        ['new'], ['describe', '-m', 'feature'],
                        ['bookmark', 'create', 'feat', '-r', '@'], ['new']):
            if command == ['describe', '-m', 'base']:
                self.write_file('a.txt', 'a\n')
            if command == ['describe', '-m', 'feature']:
                self.write_file('b.txt', 'b\n')
            self.jj(*command)

    def setUp(self):
        super().setUp()
        isolate_jj(self)

    def jj(self, *args):
        """Run jj in the workspace and return its stripped stdout."""
        return subprocess.run(['jj', '--no-pager', '--color', 'never', *args], cwd=self.ws,
                              capture_output=True, text=True, check=True).stdout.strip()

    def commit_of(self, revision):
        # Without --ignore-working-copy, jj would wait for the lock a test may hold.
        return self.jj('--ignore-working-copy', 'log', '-r', revision, '--no-graph', '-T', 'commit_id')

    def operation(self, **kwargs):
        """jj.operation with its output captured."""
        out = captured_output()
        out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)
        return jj.operation(self.ws, **kwargs)


class TestWorkspace(JjRepoTestCase):
    def test_tip_is_the_parent_of_an_empty_new_change(self):
        with self.operation() as workspace:
            self.assertEqual(workspace.tip.commit, self.commit_of('feat'))
            self.assertEqual(workspace.tip.description.strip(), 'feature')
            self.assertEqual(workspace.current_bookmark(), 'feat')
            self.assertEqual(jj.change_revision(argparse.Namespace(jj_workspace=workspace)),
                             self.commit_of('feat'))

    def test_tip_is_the_working_change_once_it_has_a_description(self):
        self.jj('describe', '-m', 'more')
        with self.operation() as workspace:
            self.assertEqual(workspace.tip.commit, self.commit_of('@'))
            with self.assertRaisesRegex(CommandError, 'exactly one bookmark'):
                workspace.current_bookmark()

    def test_default_base_is_the_tip_parent(self):
        with self.operation() as workspace:
            self.assertEqual(workspace.resolve_base(
                'HEAD~1'), self.commit_of('main'))

    def test_named_base_is_resolved_to_its_commit(self):
        with self.operation() as workspace:
            self.assertEqual(workspace.resolve_base(
                'main'), self.commit_of('main'))

    def test_snapshot_check_passes_when_jj_tracks_everything(self):
        with self.operation() as workspace:
            workspace.check_snapshot()

    def test_nested_operation_needs_ignore(self):
        with self.operation():
            with self.assertRaisesRegex(CommandError, '--ignore-jj'):
                with jj.operation(self.ws):
                    pass
            with jj.operation(self.ws, ignore=True) as nested:
                self.assertIsNone(nested)

    def test_busy_working_copy_fails_instead_of_waiting(self):
        with working_copy_lock(self.ws):
            with self.assertRaisesRegex(CommandError, 'busy'):
                with self.operation():
                    pass

    def test_git_operation_in_progress_is_refused(self):
        open(os.path.join(self.ws, '.git', 'MERGE_HEAD'), 'w').close()
        with self.assertRaisesRegex(CommandError, 'git operation is in progress'):
            with self.operation():
                pass


@unittest.skipUnless(shutil.which('jj'), 'jj is not installed')
class JjP4RepoTestCase(P4RepoTestCase):
    """git-p4son commands in a colocated jj repo on top of the synced workspace, whose main jj tracks as a bookmark."""

    def build_workspace(self):
        super().build_workspace()
        isolate_jj(self)
        self.write_config('[depot]\nroot = "//myclient"\n')
        self.commit('Add config')
        self.jj('git', 'init', '--colocate')

    def setUp(self):
        super().setUp()
        isolate_jj(self)

    def jj(self, *args):
        """Run jj in the workspace and return its stripped stdout."""
        return subprocess.run(['jj', '--no-pager', '--color', 'never', *args], cwd=self.ws,
                              capture_output=True, text=True, check=True).stdout.strip()

    def describe(self, revision, template='description.first_line()'):
        return self.jj('--ignore-working-copy', 'log', '-r', revision, '--no-graph', '-T', template)

    def p4son(self, *argv):
        """Run git-p4son with argv as the command line would; returns everything it printed."""
        with captured_output() as out, \
                mock.patch('git_p4son.cli.get_workspace_dir', return_value=self.ws):
            self.assertEqual(run_command(create_parser().parse_args(argv)), 0)
        return out.getvalue()

    def p4son_fails(self, *argv, message):
        with captured_output(), \
                mock.patch('git_p4son.cli.get_workspace_dir', return_value=self.ws), \
                self.assertRaisesRegex(CommandError, message):
            run_command(create_parser().parse_args(argv))


class TestJjNew(JjP4RepoTestCase):
    def build_workspace(self):
        super().build_workspace()
        self.jj('new', 'main', '-m', 'Change a')
        self.edit('a.txt', 'a2\n')
        self.jj('bookmark', 'create', 'feat', '-r', '@')
        self.jj('new')

    def test_new_takes_the_change_its_description_and_bookmark_from_jj(self):
        self.p4son('new')
        self.assertEqual(self.p4.pending,
                         {'101': ['Change a', '', 'Changes included:', '1. Change a']})
        self.assertEqual(self.p4.opened, {'a.txt': ('101', 'edit')})
        self.assertEqual(load_changelist_alias('feat', self.ws), '101')
        self.assertEqual(self.read('a.txt'), 'a2\n')

    def test_new_takes_a_described_working_change_git_has_not_committed(self):
        self.jj('describe', '-m', 'Change b')
        self.edit('b.txt', 'b2\n')
        self.jj('bookmark', 'set', 'feat', '-r', '@', '--allow-backwards')
        self.p4son('new', '-b', 'main')
        self.assertEqual(self.p4.pending['101'],
                         ['Change b', '', 'Changes included:', '1. Change a', '2. Change b'])
        self.assertEqual(self.p4.opened, {'a.txt': (
            '101', 'edit'), 'b.txt': ('101', 'edit')})
        self.assertEqual(self.read('b.txt'), 'b2\n')

    def test_review_needs_ignore_jj(self):
        self.p4son_fails('review', 'feat', message='--ignore-jj')

    def test_update_per_commit_needs_ignore_jj(self):
        self.p4son_fails('update', '101', '--per-commit',
                         message='--ignore-jj')


class TestJjSync(JjP4RepoTestCase):
    def build_workspace(self):
        super().build_workspace()
        self.jj('new', 'main')

    def test_sync_moves_the_bookmark_to_each_sync_commit(self):
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.p4.submit(102, {'c.txt': 'c1\n'})
        self.p4son('sync', '101', '102')
        self.assertEqual(self.describe('main'),
                         'git-p4son: p4 sync //myclient/...@102')
        self.assertEqual(self.describe('main-'),
                         'git-p4son: p4 sync //myclient/...@101')
        self.assertEqual(self.describe('@-', 'commit_id'),
                         self.describe('main', 'commit_id'))
        self.assertEqual(self.read('c.txt'), 'c1\n')

    def test_sync_needs_an_empty_working_change(self):
        self.jj('describe', '-m', 'work in progress')
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.p4son_fails('sync', message='jj new')


if __name__ == '__main__':
    unittest.main()
