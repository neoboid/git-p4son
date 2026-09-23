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


class TestCommandBatch(unittest.TestCase):
    """A batch logs one summary line instead of each command inside."""

    def _run_batch(self, log, inner=2):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with log.command_batch('git ls-files -- <9 paths in 2 batches>'):
                for i in range(inner):
                    log.command(f'git ls-files -- f{i}',
                                truncate_for_spinner=True)
                    log.start_spinner()
                    log.stop_spinner()
        return buffer.getvalue()

    def test_only_the_summary_is_printed(self):
        output = self._run_batch(Log())
        self.assertEqual(output, '> git ls-files -- <9 paths in 2 batches>\n')

    def test_verbose_mode_prints_every_command(self):
        log = Log()
        log.verbose_mode = True
        output = self._run_batch(log)
        self.assertEqual(
            output, '> git ls-files -- f0\n> git ls-files -- f1\n')

    def test_nested_batches_print_only_the_outer_summary(self):
        log = Log()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with log.command_batch('outer'):
                with log.command_batch('inner'):
                    log.command('git status')
        self.assertEqual(buffer.getvalue(), '> outer\n')

    def test_an_error_inside_still_ends_the_batch(self):
        log = Log()
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            with self.assertRaises(RuntimeError):
                with log.command_batch('summary'):
                    raise RuntimeError('boom')
            log.command('git status')
            log.end_command()
        self.assertEqual(buffer.getvalue(), '> summary\n> git status\n')

    @mock.patch('git_p4son.log._is_tty', return_value=True)
    def test_inner_commands_do_not_stop_the_batch_spinner(self, _tty):
        log = Log()
        with contextlib.redirect_stdout(io.StringIO()):
            with log.command_batch('summary'):
                spinner = log._spinner_thread
                self.assertIsNotNone(spinner)
                log.command('git status', truncate_for_spinner=True)
                log.start_spinner()
                log.stop_spinner()
                self.assertIs(log._spinner_thread, spinner)
                self.assertTrue(spinner.is_alive())
            self.assertIsNone(log._spinner_thread)


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
