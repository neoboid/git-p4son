"""Tests for git_p4son.common module."""

import contextlib
import io
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

from git_p4son.common import (
    CommandError,
    join_command_line,
    prompt_choice,
    prompt_menu,
    run,
    run_with_stream_output,
    RunError,
    RunResult,
)


def _ignore_output(line, stream):
    pass


class TestRunSetsPWD(unittest.TestCase):
    @mock.patch('subprocess.run')
    def test_pwd_overrides_inherited_value(self, mock_subprocess_run):
        mock_subprocess_run.return_value = mock.Mock(
            returncode=0, stdout='', stderr='')
        with mock.patch.dict(os.environ, {'PWD': '/some/other/dir'}):
            run(['p4', 'add', 'foo.txt'], cwd='/workspace')
        env = mock_subprocess_run.call_args.kwargs['env']
        self.assertEqual(env['PWD'], '/workspace')

    @mock.patch('subprocess.run')
    def test_pwd_uses_absolute_path(self, mock_subprocess_run):
        mock_subprocess_run.return_value = mock.Mock(
            returncode=0, stdout='', stderr='')
        run(['p4', 'add', 'foo.txt'], cwd='.')
        env = mock_subprocess_run.call_args.kwargs['env']
        self.assertEqual(env['PWD'], os.path.abspath('.'))


class TestJoinCommandLine(unittest.TestCase):
    def test_simple_command(self):
        result = join_command_line(['git', 'status'])
        self.assertEqual(result, ' git status')

    def test_argument_with_spaces_is_quoted(self):
        result = join_command_line(['git', 'commit', '-m', 'hello world'])
        self.assertEqual(result, ' git commit -m "hello world"')

    def test_empty_command(self):
        result = join_command_line([])
        self.assertEqual(result, '')

    def test_single_argument(self):
        result = join_command_line(['ls'])
        self.assertEqual(result, ' ls')


class TestRunResult(unittest.TestCase):
    def test_fields(self):
        r = RunResult(0, ['line1'], ['err1'])
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout, ['line1'])
        self.assertEqual(r.stderr, ['err1'])
        self.assertIsNone(r.elapsed)

    def test_elapsed_field(self):
        from datetime import timedelta
        r = RunResult(0, [], [], elapsed=timedelta(seconds=1.5))
        self.assertEqual(r.elapsed, timedelta(seconds=1.5))


class TestRun(unittest.TestCase):
    @mock.patch('subprocess.run')
    def test_calls_subprocess_and_returns_result(self, mock_subprocess_run):
        mock_subprocess_run.return_value = mock.Mock(
            returncode=0,
            stdout='line1\nline2\n',
            stderr='',
        )
        result = run(['git', 'status'], cwd='/tmp')
        call = mock_subprocess_run.call_args
        self.assertEqual(call.args, (['git', 'status'],))
        self.assertEqual(call.kwargs['cwd'], '/tmp')
        self.assertEqual(call.kwargs['capture_output'], True)
        self.assertEqual(call.kwargs['text'], True)
        self.assertEqual(call.kwargs['input'], None)
        self.assertEqual(call.kwargs['env']['PWD'], '/tmp')
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, ['line1', 'line2'])
        self.assertEqual(result.stderr, [])

    def test_dry_run_returns_empty_result(self):
        result = run(['git', 'status'], dry_run=True)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, [])
        self.assertEqual(result.stderr, [])

    @mock.patch('subprocess.run')
    def test_nonzero_returncode_raises_run_error(self, mock_subprocess_run):
        mock_subprocess_run.return_value = mock.Mock(
            returncode=1,
            stdout='',
            stderr='error\n',
        )
        with self.assertRaises(RunError) as ctx:
            run(['p4', 'info'])
        self.assertEqual(ctx.exception.returncode, 1)
        self.assertEqual(ctx.exception.stderr, ['error'])


class TestRunWithOutput(unittest.TestCase):
    @mock.patch('subprocess.Popen')
    def test_calls_popen_and_returns_result(self, mock_popen_cls):
        mock_process = mock.MagicMock()
        mock_process.stdout.readline = mock.Mock(side_effect=['out1\n', ''])
        mock_process.stderr.readline = mock.Mock(side_effect=['err1\n', ''])
        mock_process.wait.return_value = 0
        mock_process.__enter__ = mock.Mock(return_value=mock_process)
        mock_process.__exit__ = mock.Mock(return_value=False)
        mock_popen_cls.return_value = mock_process

        result = run_with_stream_output(
            ['git', 'status'], _ignore_output, cwd='/tmp')
        call = mock_popen_cls.call_args
        self.assertEqual(call.args, (['git', 'status'],))
        self.assertEqual(call.kwargs['cwd'], '/tmp')
        self.assertEqual(call.kwargs['stdout'], subprocess.PIPE)
        self.assertEqual(call.kwargs['stderr'], subprocess.PIPE)
        self.assertEqual(call.kwargs['text'], True)
        self.assertEqual(call.kwargs['env']['PWD'], '/tmp')
        self.assertEqual(result.returncode, 0)

    def test_without_keep_stdout_lines_reach_only_the_callback(self):
        seen = []
        with mock.patch('git_p4son.common.log'):
            result = run_with_stream_output(
                [sys.executable, '-c', 'print("one"); print("two")'],
                on_output=lambda line, stream: seen.append(line),
                keep_stdout=False)
        self.assertEqual(seen, ['one', 'two'])
        self.assertEqual(result.stdout, [])

    @mock.patch('subprocess.Popen')
    def test_callback_is_invoked(self, mock_popen_cls):
        mock_process = mock.MagicMock()
        mock_process.stdout.readline = mock.Mock(side_effect=['hello\n', ''])
        mock_process.stderr.readline = mock.Mock(side_effect=[''])
        mock_process.wait.return_value = 0
        mock_process.__enter__ = mock.Mock(return_value=mock_process)
        mock_process.__exit__ = mock.Mock(return_value=False)
        mock_popen_cls.return_value = mock_process

        callback = mock.Mock()
        run_with_stream_output(['echo', 'hello'], on_output=callback)
        callback_lines = [call.kwargs.get('line', call.args[0] if call.args else None)
                          for call in callback.call_args_list]
        self.assertIn('hello', callback_lines)

    def test_captures_all_output_from_fast_exiting_process(self):
        """A process that bursts output and exits loses no trailing lines."""
        code = (
            'import sys\n'
            'sys.stdout.write("".join(f"line{i}\\n" for i in range(5000)))\n'
            'sys.stderr.write("".join(f"err{i}\\n" for i in range(500)))\n'
        )
        callback_lines = []

        def callback(line, stream):
            callback_lines.append(line)

        result = run_with_stream_output([sys.executable, '-c', code],
                                        on_output=callback)

        self.assertEqual(result.returncode, 0)
        self.assertEqual(len(result.stdout), 5000)
        self.assertEqual(result.stdout[-1], 'line4999')
        self.assertEqual(len(result.stderr), 500)
        self.assertEqual(result.stderr[-1], 'err499')
        self.assertEqual(len(callback_lines), 5500)

    def test_callback_exception_terminates_process(self):
        """Raising from on_output stops the command instead of waiting."""
        code = ('import sys, time\n'
                'print("bad", flush=True)\n'
                'time.sleep(60)\n')

        def callback(line, stream):
            raise CommandError(f'abort on {line}')

        start = time.monotonic()
        with mock.patch('git_p4son.common.log') as mock_log:
            with self.assertRaises(CommandError) as ctx:
                run_with_stream_output([sys.executable, '-c', code],
                                       on_output=callback)
        self.assertEqual(str(ctx.exception), 'abort on bad')
        self.assertLess(time.monotonic() - start, 30)
        mock_log.stop_spinner.assert_called_once()

    def test_ctrl_c_terminates_process_and_reaches_the_caller(self):
        """Ctrl-C stops the command and propagates, so cli.py reports it like any other."""
        code = ('import sys, time\n'
                'print("line", flush=True)\n'
                'time.sleep(60)\n')

        def callback(line, stream):
            raise KeyboardInterrupt

        start = time.monotonic()
        with mock.patch('git_p4son.common.log') as mock_log:
            with self.assertRaises(KeyboardInterrupt):
                run_with_stream_output([sys.executable, '-c', code],
                                       on_output=callback)
        self.assertLess(time.monotonic() - start, 30)
        mock_log.stop_spinner.assert_called_once()
        mock_log.error.assert_not_called()


class TestMissingExecutable(unittest.TestCase):
    def test_run_stops_spinner_and_raises(self):
        with mock.patch('git_p4son.common.log') as mock_log:
            with self.assertRaises(FileNotFoundError):
                run(['definitely-not-a-real-binary-p4son'])
        mock_log.stop_spinner.assert_called_once()

    def test_run_with_stream_output_stops_spinner_and_raises(self):
        with mock.patch('git_p4son.common.log') as mock_log:
            with self.assertRaises(FileNotFoundError):
                run_with_stream_output(
                    ['definitely-not-a-real-binary-p4son'], _ignore_output)
        mock_log.stop_spinner.assert_called_once()


class TestSubprocessEncoding(unittest.TestCase):
    """Output decodes as UTF-8 on every platform, not the Windows ANSI code page."""

    _CODE = 'import sys; sys.stdout.buffer.write("bäck\\n".encode("utf-8"))'

    def test_run_decodes_utf8(self):
        result = run([sys.executable, '-c', self._CODE])
        self.assertEqual(result.stdout, ['bäck'])

    def test_bytes_mode_failure_still_carries_stderr(self):
        code = 'import sys; sys.stderr.write("the reason"); sys.exit(1)'
        with self.assertRaises(RunError) as ctx:
            run([sys.executable, '-c', code], text=False)
        self.assertEqual(ctx.exception.stderr, ['the reason'])

    def test_run_with_stream_output_decodes_utf8(self):
        result = run_with_stream_output(
            [sys.executable, '-c', self._CODE], _ignore_output)
        self.assertEqual(result.stdout, ['bäck'])


class TestCommandError(unittest.TestCase):
    def test_message_and_returncode(self):
        e = CommandError('something went wrong', returncode=2)
        self.assertEqual(str(e), 'something went wrong')
        self.assertEqual(e.returncode, 2)

    def test_default_returncode(self):
        e = CommandError('oops')
        self.assertEqual(e.returncode, 1)

    def test_no_stderr_attribute(self):
        e = CommandError('oops')
        self.assertFalse(hasattr(e, 'stderr'))


class TestRunError(unittest.TestCase):
    def test_is_command_error_subclass(self):
        e = RunError('cmd failed', returncode=3, stderr=['err'])
        self.assertIsInstance(e, CommandError)

    def test_fields(self):
        e = RunError('cmd failed', returncode=3, stderr=['line1', 'line2'])
        self.assertEqual(str(e), 'cmd failed')
        self.assertEqual(e.returncode, 3)
        self.assertEqual(e.stderr, ['line1', 'line2'])

    def test_default_stderr(self):
        e = RunError('cmd failed')
        self.assertEqual(e.stderr, [])


class TestRunWithStreamOutputFailure(unittest.TestCase):
    def test_non_zero_exit_raises_run_error_with_stderr(self):
        code = 'import sys; print("cannot clobber", file=sys.stderr); sys.exit(2)'
        with mock.patch('git_p4son.common.log'), \
                self.assertRaises(RunError) as raised:
            run_with_stream_output([sys.executable, '-c', code],
                                   lambda line, stream: None)
        self.assertEqual(raised.exception.returncode, 2)
        self.assertEqual(raised.exception.stderr, ['cannot clobber'])


class TestPromptChoice(unittest.TestCase):
    def test_renders_prompt_from_options(self):
        with mock.patch('builtins.input', return_value='n') as mock_input:
            result = prompt_choice('Delete?', ['yes', 'no'])
        self.assertEqual(result, 'no')
        mock_input.assert_called_once_with('Delete? [y]es / [n]o: ')

    def test_accepts_full_word(self):
        with mock.patch('builtins.input', return_value='quit'):
            self.assertEqual(
                prompt_choice('Delete', ['all', 'quit']), 'quit')

    def test_empty_answer_picks_the_default_shown_in_capitals(self):
        with mock.patch('builtins.input', return_value='') as mock_input:
            self.assertEqual(prompt_choice('Keep?', ['yes', 'no'], 'no'), 'no')
        mock_input.assert_called_once_with('Keep? [y]es / [N]o: ')

    def test_empty_answer_without_default_asks_again(self):
        with mock.patch('builtins.input', side_effect=['', 'y']) as mock_input:
            self.assertEqual(prompt_choice('Keep?', ['yes', 'no']), 'yes')
        self.assertEqual(mock_input.call_count, 2)

    def test_options_need_distinct_first_letters(self):
        with self.assertRaises(ValueError):
            prompt_choice('Pick', ['apple', 'avocado'])


class TestPromptMenu(unittest.TestCase):
    def test_lists_options_with_aligned_descriptions(self):
        buffer = io.StringIO()
        with mock.patch('builtins.input', return_value='') as mock_input, \
                contextlib.redirect_stdout(buffer):
            result = prompt_menu('Pick one:', [('entire workspace', '//ws/...'),
                                               ('current', '//ws/sub/...'),
                                               ('quit', '')],
                                 'entire workspace')
        self.assertEqual(result, 'entire workspace')
        self.assertEqual(buffer.getvalue(), (
            'Pick one:\n'
            '  [e]ntire workspace  //ws/...\n'
            '  [c]urrent           //ws/sub/...\n'
            '  [q]uit\n'))
        mock_input.assert_called_once_with('Choice [E/c/q]: ')


if __name__ == '__main__':
    unittest.main()
