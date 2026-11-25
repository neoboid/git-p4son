"""Tests for git-p4son's own lock on a git workspace, in git_p4son.lock."""

import os
import subprocess
import sys
from unittest import mock

from git_p4son import CONFIG_DIR
from git_p4son.common import CommandError
from git_p4son.lock import _LOCK_ENV, command_lock
from tests.helpers import WorkspaceTestCase

_BUSY = 'Another git-p4son command is running in this workspace'
# Takes the lock on the workspace argv[1] names, prints its marker, and holds the lock until its stdin closes.
_HOLDER = ('import os, sys\n'
           'from git_p4son.lock import _LOCK_ENV, command_lock\n'
           'with command_lock(sys.argv[1]):\n'
           '    print(os.environ[_LOCK_ENV], flush=True)\n'
           '    sys.stdin.read()\n')


def _environment_of_another_process():
    """Without the marker of a lock held here, which only the processes the holder starts inherit."""
    return {name: value for name, value in os.environ.items() if name != _LOCK_ENV}


def _as_another_process():
    return mock.patch.dict(os.environ, _environment_of_another_process(), clear=True)


class TestCommandLock(WorkspaceTestCase):
    def lock_file(self):
        return os.path.join(self.ws, CONFIG_DIR, 'lock')

    def assert_busy(self):
        with _as_another_process(), self.assertRaisesRegex(CommandError, _BUSY):
            with command_lock(self.ws):
                pass

    def assert_free(self):
        with _as_another_process(), command_lock(self.ws):
            pass

    def holder(self):
        """A process holding the lock, ended with the test; its marker is in self.holders_marker."""
        process = subprocess.Popen([sys.executable, '-c', _HOLDER, self.ws], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, text=True)
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        assert process.stdout is not None
        self.addCleanup(process.stdout.close)
        self.holders_marker = process.stdout.readline().rstrip('\n')
        self.assertTrue(self.holders_marker)
        return process

    def under_marker(self, marker):
        """As a process that a holder with marker started, and that may have outlived it."""
        return mock.patch.dict(os.environ, {_LOCK_ENV: marker})

    def test_a_busy_lock_raises(self):
        with command_lock(self.ws):
            self.assert_busy()

    def test_the_lock_is_released_and_its_file_removed(self):
        with command_lock(self.ws):
            self.assertTrue(os.path.exists(self.lock_file()))
        self.assertFalse(os.path.exists(self.lock_file()))
        self.assert_free()

    def test_a_command_the_holder_starts_runs_under_its_lock(self):
        with command_lock(self.ws):
            with command_lock(self.ws):
                pass
            self.assert_busy()
        self.assertNotIn(_LOCK_ENV, os.environ)

    def test_a_lock_held_on_another_workspace_does_not_count(self):
        with mock.patch.dict(os.environ, {_LOCK_ENV: os.path.join(self.ws, 'elsewhere')}):
            with command_lock(self.ws):
                self.assert_busy()
            self.assertEqual(
                os.environ[_LOCK_ENV], os.path.join(self.ws, 'elsewhere'))

    def test_the_config_dir_is_made_with_its_gitignore(self):
        with command_lock(self.ws):
            with open(os.path.join(self.ws, CONFIG_DIR, '.gitignore')) as f:
                self.assertEqual(f.read(), '*\n')

    def test_another_process_holding_it_makes_it_busy_until_it_ends(self):
        process = self.holder()
        self.assert_busy()
        process.communicate('')
        self.assert_free()

    def test_a_process_that_outlives_its_holder_is_refused_while_another_holds_the_lock(self):
        """As an editor a post-sync hook started, whose terminal later runs git-p4son during someone's sync."""
        with command_lock(self.ws):
            marker = os.environ[_LOCK_ENV]
        self.holder()
        with self.under_marker(marker), self.assertRaisesRegex(CommandError, _BUSY):
            with command_lock(self.ws):
                pass

    def test_a_process_that_outlives_its_holder_takes_the_lock_when_it_is_free(self):
        with command_lock(self.ws):
            marker = os.environ[_LOCK_ENV]
        with self.under_marker(marker), command_lock(self.ws):
            self.assert_busy()

    def test_a_process_whose_holder_died_takes_the_lock(self):
        process = self.holder()
        process.kill()
        process.wait()
        with self.under_marker(self.holders_marker), command_lock(self.ws):
            self.assert_busy()

    def test_a_holder_that_dies_releases_it(self):
        process = self.holder()
        process.kill()
        process.wait()
        self.assert_free()
