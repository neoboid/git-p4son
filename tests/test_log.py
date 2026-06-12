"""Tests for git_p4son.log module."""

import contextlib
import io
import os
import unittest
from unittest import mock

from git_p4son.log import Log, _truncate_to_terminal_width


class TestNonTtyOutput(unittest.TestCase):
    def test_command_output_is_clean_when_redirected(self):
        """Redirected output (git p4son sync > log.txt) must not contain
        spinner frames, carriage returns, or escape sequences."""
        log = Log()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            log.command('git status', truncate_for_spinner=True)
            log.start_spinner()
            log.stop_spinner()
        self.assertEqual(buffer.getvalue(), '> git status\n')

    def test_spinner_not_started_without_tty(self):
        log = Log()
        with contextlib.redirect_stdout(io.StringIO()):
            log.start_spinner()
            self.assertIsNone(log._spinner_thread)
            log.stop_spinner()

    def test_input_command_line_not_doubled(self):
        """The input path calls end_command explicitly; the line is
        already terminated in non-TTY mode."""
        log = Log()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            log.command('p4 change -i')
            log.end_command()
        self.assertEqual(buffer.getvalue(), '> p4 change -i\n')


class TestCommandTruncation(unittest.TestCase):
    @mock.patch('git_p4son.log.shutil.get_terminal_size')
    def test_truncates_to_leave_room_for_spinner(self, mock_terminal_size):
        mock_terminal_size.return_value = os.terminal_size((40, 20))
        line = '> powershell.exe ' + ('x' * 80) + 'hook.ps1'

        truncated = _truncate_to_terminal_width(line)

        self.assertLessEqual(len(truncated), 38)
        self.assertTrue(truncated.startswith('> powershell'))
        self.assertTrue(truncated.endswith('hook.ps1'))
        self.assertIn(' ... ', truncated)

    @mock.patch('git_p4son.log.shutil.get_terminal_size')
    @mock.patch('sys.stdout')
    def test_command_keeps_full_final_line(self, _stdout, mock_terminal_size):
        mock_terminal_size.return_value = os.terminal_size((40, 20))
        log = Log()
        command = 'powershell.exe ' + ('x' * 80) + 'hook.ps1'

        log.command(command, truncate_for_spinner=True)

        self.assertIn(' ... ', log._spinner_line)
        self.assertEqual(log._spinner_final_line, f'> {command}')


if __name__ == '__main__':
    unittest.main()
