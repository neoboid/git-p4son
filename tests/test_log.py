"""Tests for git_p4son.log module."""

import os
import unittest
from unittest import mock

from git_p4son.log import Log, _truncate_to_terminal_width


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
