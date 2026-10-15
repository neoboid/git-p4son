"""Tests for git_p4son.processes module."""

import unittest
from unittest import mock

from git_p4son.common import CommandError, RunError
from git_p4son.config import ConfigError
from git_p4son.processes import (
    check_no_blocking_processes,
    get_blocking_processes,
    get_running_processes,
    normalize_process_name,
)
from tests.helpers import make_run_result, WorkspaceTestCase


class TestGetBlockingProcesses(WorkspaceTestCase):

    def test_no_config_file(self):
        self.assertEqual(get_blocking_processes(self.ws), [])

    def test_no_sync_table(self):
        self.write_config('[depot]\nroot = "//ws"\n')
        self.assertEqual(get_blocking_processes(self.ws), [])

    def test_no_key(self):
        self.write_config('[sync]\nother = 1\n')
        self.assertEqual(get_blocking_processes(self.ws), [])

    def test_names(self):
        self.write_config(
            '[sync]\nblocking-processes = ["UnrealEditor", "Lightmass"]\n')
        self.assertEqual(get_blocking_processes(self.ws),
                         ['UnrealEditor', 'Lightmass'])

    def test_not_a_list(self):
        self.write_config('[sync]\nblocking-processes = "UnrealEditor"\n')
        with self.assertRaises(ConfigError):
            get_blocking_processes(self.ws)

    def test_non_string_entry(self):
        self.write_config('[sync]\nblocking-processes = ["a", 1]\n')
        with self.assertRaises(ConfigError):
            get_blocking_processes(self.ws)

    def test_sync_not_a_table(self):
        self.write_config('sync = 1\n')
        with self.assertRaises(ConfigError):
            get_blocking_processes(self.ws)


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


class TestCheckNoBlockingProcesses(WorkspaceTestCase):

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
        check_no_blocking_processes(self.ws, False)
        self.mock_running.assert_not_called()

    def test_none_running(self):
        self.write_config('[sync]\nblocking-processes = ["Lightmass"]\n')
        check_no_blocking_processes(self.ws, False)
        self.mock_log.success.assert_called_once_with('None running')

    def test_running_blocks(self):
        self.write_config(
            '[sync]\nblocking-processes = ["UnrealEditor.exe", "Lightmass"]\n')
        with self.assertRaisesRegex(CommandError, '--ignore-blocking-processes'):
            check_no_blocking_processes(self.ws, False)
        self.mock_log.info.assert_called_once_with('UnrealEditor.exe')

    def test_ignore_skips_listing(self):
        self.write_config('[sync]\nblocking-processes = ["UnrealEditor"]\n')
        check_no_blocking_processes(self.ws, True)
        self.mock_running.assert_not_called()

    def test_ignore_skips_malformed_config(self):
        self.write_config('[sync]\nblocking-processes = "UnrealEditor"\n')
        check_no_blocking_processes(self.ws, True)

    def test_malformed_config_fails(self):
        self.write_config('[sync]\nblocking-processes = "UnrealEditor"\n')
        with self.assertRaisesRegex(ConfigError, 'blocking-processes'):
            check_no_blocking_processes(self.ws, False)

    def test_invalid_toml_fails(self):
        self.write_config('[sync\n')
        with self.assertRaisesRegex(ConfigError, 'not valid TOML'):
            check_no_blocking_processes(self.ws, False)

    def test_listing_failure_fails(self):
        self.write_config('[sync]\nblocking-processes = ["UnrealEditor"]\n')
        for error in (OSError('no ps'), RunError('ps failed')):
            self.mock_running.side_effect = error
            with self.assertRaisesRegex(CommandError,
                                        'Could not list running processes'):
                check_no_blocking_processes(self.ws, False)
