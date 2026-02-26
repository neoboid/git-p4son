"""Tests for git_p4son.log module."""

import sys
import time
from datetime import timedelta
from unittest.mock import patch

from git_p4son.log import Color, Log


class TestHeading:
    def test_first_heading_no_blank_line(self, capsys):
        log = Log()
        log.heading('First')
        out = capsys.readouterr().out
        assert out == '# First\n'

    def test_second_heading_has_blank_line(self, capsys):
        log = Log()
        log.heading('First')
        log.heading('Second')
        out = capsys.readouterr().out
        assert out == '# First\n\n# Second\n'

    def test_third_heading_has_blank_line(self, capsys):
        log = Log()
        log.heading('A')
        log.heading('B')
        log.heading('C')
        out = capsys.readouterr().out
        assert out == '# A\n\n# B\n\n# C\n'


class TestCommand:
    def test_command_no_newline(self, capsys):
        log = Log()
        log.command('p4 info')
        out = capsys.readouterr().out
        assert out == '> p4 info'

    def test_end_command(self, capsys):
        log = Log()
        log.command('p4 info')
        log.end_command()
        out = capsys.readouterr().out
        assert out == '> p4 info\n'


class TestDetail:
    def test_detail(self, capsys):
        log = Log()
        log.detail('root', '/Users/me/project')
        out = capsys.readouterr().out
        assert out == 'root: /Users/me/project\n'

    def test_detail_with_int_value(self, capsys):
        log = Log()
        log.detail('last synced', 54320)
        out = capsys.readouterr().out
        assert out == 'last synced: 54320\n'


class TestInfo:
    def test_info(self, capsys):
        log = Log()
        log.info('clean')
        out = capsys.readouterr().out
        assert out == 'clean\n'


class TestVerbose:
    def test_verbose_suppressed_by_default(self, capsys):
        log = Log()
        log.verbose('debug info')
        out = capsys.readouterr().out
        assert out == ''

    def test_verbose_shown_when_enabled(self, capsys):
        log = Log()
        log.verbose_mode = True
        log.verbose('debug info')
        out = capsys.readouterr().out
        assert out == 'debug info\n'


class TestStdin:
    def test_stdin_suppressed_by_default(self, capsys):
        log = Log()
        log.stdin('Change: new\n\nDescription:\n\tFix bug')
        out = capsys.readouterr().out
        assert out == ''

    def test_stdin_shown_when_verbose(self, capsys):
        log = Log()
        log.verbose_mode = True
        log.stdin('line1\nline2')
        out = capsys.readouterr().out
        assert out == 'stdin:\n  line1\n  line2\n'


class TestElapsed:
    def test_elapsed(self, capsys):
        log = Log()
        log.elapsed(timedelta(seconds=5.123))
        out = capsys.readouterr().out
        assert out == 'elapsed: 0:00:05.123000\n'


class TestError:
    def test_error_goes_to_stderr(self, capsys):
        log = Log()
        log.error('something went wrong')
        err = capsys.readouterr().err
        assert err == '[err] something went wrong\n'

    def test_error_not_on_stdout(self, capsys):
        log = Log()
        log.error('something went wrong')
        out = capsys.readouterr().out
        assert out == ''


class TestFileChange:
    def test_add_prefix(self, capsys):
        log = Log()
        log.file_change('new.txt', 'add')
        out = capsys.readouterr().out
        assert out == '  + new.txt\n'

    def test_delete_prefix(self, capsys):
        log = Log()
        log.file_change('old.txt', 'delete')
        out = capsys.readouterr().out
        assert out == '  - old.txt\n'

    def test_modify_prefix(self, capsys):
        log = Log()
        log.file_change('changed.txt', 'modify')
        out = capsys.readouterr().out
        assert out == '  ~ changed.txt\n'

    def test_untracked_prefix(self, capsys):
        log = Log()
        log.file_change('unknown.txt', 'untracked')
        out = capsys.readouterr().out
        assert out == '  ? unknown.txt\n'

    def test_add_has_green_color(self, capsys):
        log = Log()
        with patch.object(sys.stdout, 'isatty', return_value=True):
            log.file_change('new.txt', 'add')
        out = capsys.readouterr().out
        assert out == f'  {Color.ADD}+{Color.RESET} new.txt\n'

    def test_delete_has_red_color(self, capsys):
        log = Log()
        with patch.object(sys.stdout, 'isatty', return_value=True):
            log.file_change('old.txt', 'delete')
        out = capsys.readouterr().out
        assert out == f'  {Color.DELETE}-{Color.RESET} old.txt\n'


class TestFail:
    def test_fail_goes_to_stderr(self, capsys):
        log = Log()
        log.fail(1)
        err = capsys.readouterr().err
        assert err == 'Failed with return code 1\n'

    def test_fail_with_other_code(self, capsys):
        log = Log()
        log.fail(42)
        err = capsys.readouterr().err
        assert err == 'Failed with return code 42\n'


class TestSpinner:
    def test_start_and_stop_spinner(self):
        log = Log()
        log._spinner_line = '> p4 sync'
        log.start_spinner()
        assert log._spinner_thread is not None
        assert log._spinner_thread.is_alive()
        log.stop_spinner()
        assert log._spinner_thread is None

    def test_stop_spinner_when_not_started(self):
        log = Log()
        # Should not raise
        log.stop_spinner()
        assert log._spinner_thread is None

    def test_stop_spinner_reprints_clean_line(self):
        log = Log()
        log._spinner_line = '> p4 sync'
        log.start_spinner()
        # Let spinner run briefly
        time.sleep(0.15)
        with patch('sys.stdout') as mock_stdout:
            mock_stdout.isatty.return_value = False
            log.stop_spinner()
            # stop_spinner should write the clean line + newline
            calls = mock_stdout.write.call_args_list
            assert any('> p4 sync' in str(c) for c in calls)

    def test_spinner_thread_is_daemon(self):
        log = Log()
        log._spinner_line = '> cmd'
        log.start_spinner()
        assert log._spinner_thread.daemon is True
        log.stop_spinner()


class TestColor:
    """Tests for ANSI color output when isatty returns True."""

    def test_heading_has_color(self, capsys):
        log = Log()
        with patch.object(sys.stdout, 'isatty', return_value=True):
            log.heading('Sync')
        out = capsys.readouterr().out
        assert out == f'{Color.HEADING}# Sync{Color.RESET}\n'

    def test_command_has_colored_prompt(self, capsys):
        log = Log()
        with patch.object(sys.stdout, 'isatty', return_value=True):
            log.command('p4 info')
        out = capsys.readouterr().out
        assert out == f'{Color.COMMAND}>{Color.RESET} p4 info'

    def test_command_spinner_line_is_plain(self):
        log = Log()
        with patch.object(sys.stdout, 'isatty', return_value=True):
            log.command('p4 sync')
        assert log._spinner_line == '> p4 sync'

    def test_error_has_colored_prefix(self, capsys):
        log = Log()
        with patch.object(sys.stderr, 'isatty', return_value=True):
            log.error('something broke')
        err = capsys.readouterr().err
        assert err == f'[{Color.ERROR}err{Color.RESET}] something broke\n'

    def test_fail_has_colored_prefix(self, capsys):
        log = Log()
        with patch.object(sys.stderr, 'isatty', return_value=True):
            log.fail(1)
        err = capsys.readouterr().err
        assert err == f'{Color.FAIL}Failed{Color.RESET} with return code 1\n'

    def test_no_color_without_tty(self, capsys):
        """Capsys is not a TTY, so no ANSI codes should appear."""
        log = Log()
        log.heading('Test')
        log.command('cmd')
        out = capsys.readouterr().out
        assert '\033[' not in out
