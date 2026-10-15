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
from tests.helpers import captured_output, WorkspaceTestCase


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


if __name__ == '__main__':
    unittest.main()
