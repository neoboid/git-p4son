"""Tests for git_p4son.log module."""

import contextlib
import io
import os
import re
import subprocess
import sys
import time
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


class _Terminal(io.StringIO):
    """A stdout that reports itself as a terminal."""

    def isatty(self):
        return True


def _rendered(text):
    """The lines a terminal shows for text: carriage returns rewrite the line, colors and clear-to-end vanish."""
    lines = ['']
    for part in re.split(r'(\r|\n|\x1b\[K)', re.sub(r'\x1b\[[0-9;]*m', '', text)):
        if part == '\n':
            lines.append('')
        elif part == '\r':
            lines[-1] = ''
        elif part and part != '\x1b[K':
            lines[-1] += part
    return [line for line in lines if line]


class TestMessageDuringCommand(unittest.TestCase):
    """A message logged while a command line is open ends the line first, so it doesn't land on it."""

    def run_command(self, log_message, spin=True):
        log = Log()
        terminal = _Terminal()
        with contextlib.redirect_stdout(terminal):
            log.command('p4 sync -n //ws/...@100', truncate_for_spinner=spin)
            if spin:
                log.start_spinner()
                # Long enough for the spinner to draw a frame.
                time.sleep(0.25)
            log_message(log)
            log.stop_spinner()
            log.info('after')
        return _rendered(terminal.getvalue())

    def test_info_goes_below_the_spinner_s_command_line(self):
        self.assertEqual(self.run_command(lambda log: log.info('All files up to date')),
                         ['> p4 sync -n //ws/...@100', 'All files up to date', 'after'])

    def test_a_warning_and_a_success_too(self):
        self.assertEqual(self.run_command(lambda log: (log.warning('Unparsable line: x'), log.success('done'))),
                         ['> p4 sync -n //ws/...@100', '[warn] Unparsable line: x', '[ok] done', 'after'])

    def test_a_command_line_left_open_without_a_spinner(self):
        self.assertEqual(self.run_command(lambda log: log.info('message'), spin=False),
                         ['> p4 sync -n //ws/...@100', 'message', 'after'])


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


class TestMergedStreams(unittest.TestCase):
    def test_stderr_lines_keep_their_place_after_stdout_lines(self):
        """With 2>&1 into a pipe, stdout is block-buffered, so stderr would jump ahead unless stdout is flushed."""
        script = ('from git_p4son.log import log\n'
                  'log.info("one")\n'
                  'log.error("two")\n'
                  'log.info("three")\n'
                  'log.output("four", stderr=True)\n')
        result = subprocess.run([sys.executable, '-c', script], stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True, check=True,
                                cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        self.assertEqual(result.stdout, 'one\n[err] two\nthree\nfour\n')


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
