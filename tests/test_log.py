"""Tests for git_p4son.log module."""

import contextlib
import io
import os
import unittest
from unittest import mock

from git_p4son.log import Log, _truncate_to_terminal_width, plural


class TestNonTtyOutput(unittest.TestCase):
    def test_command_output_is_clean_when_redirected(self):
        """Redirected output has no spinner frames, carriage returns or escape sequences."""
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
        """The input command line is not terminated twice in non-TTY mode."""
        log = Log()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            log.command('p4 change -i')
            log.end_command()
        self.assertEqual(buffer.getvalue(), '> p4 change -i\n')


class TestPlural(unittest.TestCase):
    def test_one_is_singular(self):
        self.assertEqual(plural(1, 'file'), '1 file')

    def test_other_counts_add_an_s(self):
        self.assertEqual(plural(0, 'file'), '0 files')
        self.assertEqual(plural(3, 'file'), '3 files')

    def test_irregular_plural(self):
        self.assertEqual(plural(2, 'alias', 'aliases'), '2 aliases')


class TestOutput(unittest.TestCase):
    def test_writes_lines_as_is_to_the_given_stream(self):
        log = Log()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            log.output('> not a command')
            log.output('a warning', stderr=True)
        self.assertEqual(out.getvalue(), '> not a command\n')
        self.assertEqual(err.getvalue(), 'a warning\n')

    def test_silent_in_quiet_mode(self):
        log = Log()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), log.quiet():
            log.output('line')
            log.output('line', stderr=True)
        self.assertEqual((out.getvalue(), err.getvalue()), ('', ''))


class TestQuiet(unittest.TestCase):
    def test_silences_the_block_then_restores_the_mode(self):
        log = Log()
        with log.quiet():
            self.assertTrue(log.quiet_mode)
        self.assertFalse(log.quiet_mode)

    def test_restores_the_mode_when_the_block_raises(self):
        log = Log()
        with self.assertRaises(ValueError), log.quiet():
            raise ValueError
        self.assertFalse(log.quiet_mode)

    def test_disabled_leaves_an_outer_quiet_on(self):
        log = Log()
        with log.quiet():
            with log.quiet(False):
                self.assertTrue(log.quiet_mode)
            self.assertTrue(log.quiet_mode)


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
