"""Tests for git_p4son.processes module."""

import os
import tempfile
import unittest
from unittest import mock

from git_p4son.common import RunError
from git_p4son.processes import (
    check_no_blocking_processes, get_blocking_processes,
    get_running_processes, normalize_process_name,
)
from tests.helpers import make_run_result


class _WorkspaceTestCase(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.workspace = self._tmp.name

    def write_config(self, text):
        config_dir = os.path.join(self.workspace, '.git-p4son')
        os.makedirs(config_dir, exist_ok=True)
        with open(os.path.join(config_dir, 'config.toml'), 'w',
                  encoding='utf-8') as f:
            f.write(text)


class TestGetBlockingProcesses(_WorkspaceTestCase):

    def test_no_config_file(self):
        self.assertEqual(get_blocking_processes(self.workspace), [])

    def test_no_sync_table(self):
        self.write_config('[depot]\nroot = "//ws"\n')
        self.assertEqual(get_blocking_processes(self.workspace), [])

    def test_no_key(self):
        self.write_config('[sync]\nother = 1\n')
        self.assertEqual(get_blocking_processes(self.workspace), [])

    def test_names(self):
        self.write_config(
            '[sync]\nblocking-processes = ["UnrealEditor", "Lightmass"]\n')
        self.assertEqual(get_blocking_processes(self.workspace),
                         ['UnrealEditor', 'Lightmass'])

    def test_not_a_list(self):
        self.write_config('[sync]\nblocking-processes = "UnrealEditor"\n')
        with self.assertRaises(ValueError):
            get_blocking_processes(self.workspace)

    def test_non_string_entry(self):
        self.write_config('[sync]\nblocking-processes = ["a", 1]\n')
        with self.assertRaises(ValueError):
            get_blocking_processes(self.workspace)

    def test_sync_not_a_table(self):
        self.write_config('sync = 1\n')
        with self.assertRaises(ValueError):
            get_blocking_processes(self.workspace)


class TestNormalizeProcessName(unittest.TestCase):

    def test_strips_exe_and_case(self):
        self.assertEqual(normalize_process_name('UnrealEditor.EXE'),
                         'unrealeditor')

    def test_strips_directory_and_whitespace(self):
        self.assertEqual(
            normalize_process_name(
                '  /Applications/Foo.app/Contents/MacOS/Foo \n'),
            'foo')

    def test_exe_only_stripped_as_suffix(self):
        self.assertEqual(normalize_process_name('exeditor'), 'exeditor')


class TestGetRunningProcesses(unittest.TestCase):

    @mock.patch('git_p4son.processes.os.name', 'posix')
    @mock.patch('git_p4son.processes.run')
    def test_ps(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=['/usr/bin/zsh', '', 'UnrealEditor'])
        self.assertEqual(get_running_processes(), {'zsh', 'unrealeditor'})
        self.assertEqual(mock_run.call_args.args[0][0], 'ps')

    @mock.patch('git_p4son.processes.os.name', 'nt')
    @mock.patch('git_p4son.processes.run')
    def test_tasklist(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '',
            '"UnrealEditor.exe","1234","Console","1","2,000 K"',
            '"My, App.exe","42","Console","1","100 K"',
        ])
        self.assertEqual(get_running_processes(),
                         {'unrealeditor', 'my, app'})
        self.assertEqual(mock_run.call_args.args[0][0], 'tasklist')


class TestCheckNoBlockingProcesses(_WorkspaceTestCase):

    def setUp(self):
        super().setUp()
        patcher = mock.patch('git_p4son.processes.log')
        self.mock_log = patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch('git_p4son.processes.get_running_processes',
                             return_value={'zsh', 'unrealeditor'})
        self.mock_running = patcher.start()
        self.addCleanup(patcher.stop)

    def test_nothing_configured_lists_nothing(self):
        self.assertTrue(check_no_blocking_processes(self.workspace, False))
        self.mock_running.assert_not_called()

    def test_none_running(self):
        self.write_config('[sync]\nblocking-processes = ["Lightmass"]\n')
        self.assertTrue(check_no_blocking_processes(self.workspace, False))

    def test_running_blocks(self):
        self.write_config(
            '[sync]\nblocking-processes = ["UnrealEditor.exe", "Lightmass"]\n')
        self.assertFalse(check_no_blocking_processes(self.workspace, False))
        self.mock_log.info.assert_called_once_with('UnrealEditor.exe')

    def test_ignore_skips_listing(self):
        self.write_config('[sync]\nblocking-processes = ["UnrealEditor"]\n')
        self.assertTrue(check_no_blocking_processes(self.workspace, True))
        self.mock_running.assert_not_called()

    def test_ignore_skips_malformed_config(self):
        self.write_config('[sync]\nblocking-processes = "UnrealEditor"\n')
        self.assertTrue(check_no_blocking_processes(self.workspace, True))

    def test_malformed_config_fails(self):
        self.write_config('[sync]\nblocking-processes = "UnrealEditor"\n')
        self.assertFalse(check_no_blocking_processes(self.workspace, False))

    def test_invalid_toml_fails(self):
        self.write_config('[sync\n')
        self.assertFalse(check_no_blocking_processes(self.workspace, False))

    def test_listing_failure_fails(self):
        self.write_config('[sync]\nblocking-processes = ["UnrealEditor"]\n')
        for error in (OSError('no ps'), RunError('ps failed')):
            self.mock_running.side_effect = error
            self.assertFalse(
                check_no_blocking_processes(self.workspace, False))
