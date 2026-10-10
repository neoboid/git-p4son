"""Common utilities shared between git-p4son commands."""

import ntpath
import os
import posixpath
import queue
import re
import subprocess
import sys
import threading
import time
from contextlib import AbstractContextManager, nullcontext
from timeit import default_timer as timer
from datetime import timedelta
from typing import IO, Callable

from .log import log


def _env_with_pwd(cwd: str) -> dict[str, str]:
    """Return a copy of os.environ with PWD set to abspath(cwd), which 'p4 add' resolves paths against."""
    env = os.environ.copy()
    env['PWD'] = os.path.abspath(cwd)
    return env


def branch_to_alias(branch_name: str) -> str:
    """Sanitize a branch name for use as an alias filename."""
    return branch_name.replace('/', '-')


def prompt_choice(prefix: str, options: list[str]) -> str | None:
    """Prompt until the response matches an option or its first letter; None on EOF."""
    shorthands = {option[0]: option for option in options}
    rendered = ' / '.join(f'[{option[0]}]{option[1:]}' for option in options)
    keys = list(shorthands)
    while True:
        try:
            response = input(f'{prefix} {rendered}: ').strip().lower()
        except EOFError:
            print()
            return None

        if response in shorthands:
            return shorthands[response]
        elif response in options:
            return response
        else:
            print('Please enter ' + ', '.join(keys[:-1]) + f' or {keys[-1]}')


def _path_module_for(*paths: str):
    """Choose a path module that matches the given path strings."""
    if any('\\' in path or re.match(r'^[A-Za-z]:', path) for path in paths):
        return ntpath
    return posixpath


def normalize_workspace_path(filename: str, workspace_dir: str,
                             allow_outside: bool = False) -> str | None:
    """Return filename as a workspace-relative slash path."""
    pathmod = _path_module_for(filename, workspace_dir)
    normalized_workspace = pathmod.normpath(workspace_dir)
    normalized_filename = pathmod.normpath(filename)

    if pathmod.isabs(normalized_filename):
        try:
            common = pathmod.commonpath([
                pathmod.normcase(normalized_workspace),
                pathmod.normcase(normalized_filename),
            ])
        except ValueError:
            common = None

        if common == pathmod.normcase(normalized_workspace):
            normalized_filename = pathmod.relpath(
                normalized_filename, normalized_workspace)
        elif not allow_outside:
            return None

    if not allow_outside:
        parts = normalized_filename.split(pathmod.sep)
        if parts and parts[0] == '..':
            return None

    return normalized_filename.replace('\\', '/')


class CommandError(Exception):
    """Raised for logic/validation errors in commands."""

    def __init__(self, message: str, returncode: int = 1) -> None:
        super().__init__(message)
        self.returncode = returncode


class RunError(CommandError):
    """Raised when a subprocess command fails."""

    def __init__(self, message: str, returncode: int = 1, stderr: list[str] | None = None) -> None:
        super().__init__(message, returncode)
        self.stderr = stderr or []


class RunResult:
    """Result of a command execution."""

    def __init__(self, returncode: int,
                 stdout: list[str] | bytes,
                 stderr: list[str] | bytes,
                 elapsed: timedelta | None = None) -> None:
        self.returncode: int = returncode
        self.stdout: list[str] | bytes = stdout
        self.stderr: list[str] | bytes = stderr
        self.elapsed: timedelta | None = elapsed


# File lists after `--` longer than this are logged as a count, except in verbose mode.
_MAX_LOGGED_PATHS = 3


def join_command_line(command: list[str]) -> str:
    command_line = ''
    for c in command:
        if ' ' in c:
            command_line += f' "{c}"'
        else:
            command_line += f' {c}'
    return command_line


def _command_line_for_log(command: list[str]) -> str:
    """The command line as printed, with a long path list summarized."""
    if log.verbose_mode or '--' not in command:
        return join_command_line(command)
    separator = command.index('--')
    paths = command[separator + 1:]
    if len(paths) <= _MAX_LOGGED_PATHS:
        return join_command_line(command)
    return join_command_line(command[:separator + 1]) + f' <{len(paths)} paths>'


def batched_command_log(prefix: list[str], path_count: int,
                        batch_count: int) -> AbstractContextManager[None]:
    """Log a command run over a path list in several batches as one line."""
    if batch_count <= 1:
        return nullcontext()
    return log.command_batch(
        join_command_line(prefix)
        + f' <{path_count} paths in {batch_count} batches>')


def run(command: list[str], cwd: str = '.', dry_run: bool = False,
        input: str | None = None,
        env: dict[str, str] | None = None,
        text: bool = True,
        fail_on_returncode: bool = True
        ) -> RunResult:
    """Run a command and return a RunResult, raising on failure unless fail_on_returncode is False."""
    use_spinner = input is None and not dry_run
    log.command(_command_line_for_log(command),
                truncate_for_spinner=use_spinner)

    if dry_run:
        log.end_command()
        return RunResult(0, [] if text else b'', [] if text else b'')

    if input is not None:
        log.end_command()
        log.stdin(input)
    else:
        log.start_spinner()

    start_timestamp = timer()

    command_env = _env_with_pwd(cwd)
    if env:
        command_env.update(env)

    # Decode as UTF-8: git emits it, but Windows would decode with cp1252.
    try:
        result = subprocess.run(command,
                                cwd=cwd,
                                env=command_env,
                                capture_output=True,
                                text=text,
                                encoding='utf-8' if text else None,
                                errors='replace' if text else None,
                                input=input if text or input is None
                                else input.encode('utf-8'))
    except OSError:
        # E.g. a missing executable: stop the spinner overwriting the error.
        log.stop_spinner()
        raise

    end_timestamp = timer()
    elapsed = timedelta(seconds=end_timestamp - start_timestamp)

    log.stop_spinner()

    if fail_on_returncode and result.returncode != 0:
        if text:
            stderr = result.stderr.splitlines()
        else:
            # Bytes mode: decode so the error still carries the reason.
            stderr = result.stderr.decode('utf-8', 'replace').splitlines()
        raise RunError(
            join_command_line(command),
            returncode=result.returncode,
            stderr=stderr,
        )

    if text:
        return RunResult(result.returncode, result.stdout.splitlines(),
                         result.stderr.splitlines(), elapsed=elapsed)
    return RunResult(result.returncode, result.stdout,
                     result.stderr, elapsed=elapsed)


def enqueue_lines(stream: IO[str], output_queue: queue.Queue[str]) -> None:
    """Enqueue lines from a stream into a queue."""
    for line in iter(stream.readline, ''):
        output_queue.put(line.rstrip())


def _terminate(process: subprocess.Popen) -> None:
    """Terminate a subprocess, killing it if it does not exit in time."""
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.error("Subprocess did not terminate in time. Forcing kill...")
        process.kill()


def run_with_output(command: list[str], cwd: str = '.',
                    on_output: Callable[..., None] | None = None,
                    env: dict[str, str] | None = None) -> RunResult:
    """Run a command, passing each output line and its stream to on_output as it is written.

    An exception raised from on_output terminates the command and propagates."""
    log.command(_command_line_for_log(command),
                truncate_for_spinner=True)
    log.start_spinner()

    start_timestamp = timer()

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    returncode: int | None = None

    command_env = _env_with_pwd(cwd)
    if env:
        command_env.update(env)

    # UTF-8 as in run(); a decode error would silently kill the reader threads.
    try:
        process_cm = subprocess.Popen(command,
                                      cwd=cwd,
                                      env=command_env,
                                      stdout=subprocess.PIPE,
                                      stderr=subprocess.PIPE,
                                      text=True,
                                      encoding='utf-8',
                                      errors='replace')
    except OSError:
        # E.g. a missing executable: stop the spinner overwriting the error.
        log.stop_spinner()
        raise

    with process_cm as process:

        output_queue: queue.Queue[str] = queue.Queue()
        out_thread = threading.Thread(
            target=enqueue_lines, args=(process.stdout, output_queue))
        out_thread.daemon = True

        error_queue: queue.Queue[str] = queue.Queue()
        err_thread = threading.Thread(
            target=enqueue_lines, args=(process.stderr, error_queue))
        err_thread.daemon = True

        out_thread.start()
        err_thread.start()

        def drain_queue(q, lines, stream):
            try:
                while not q.empty():
                    line = q.get_nowait()
                    lines.append(line)
                    if on_output:
                        on_output(line=line, stream=stream)
            except queue.Empty:
                pass

        try:
            # Loop on the readers, not process.poll(): the process can exit with output still buffered.
            while out_thread.is_alive() or err_thread.is_alive():
                drain_queue(output_queue, stdout_lines, sys.stdout)
                drain_queue(error_queue, stderr_lines, sys.stderr)
                time.sleep(0.05)

            out_thread.join()
            err_thread.join()

            # Final drain for lines enqueued after the last pass above.
            drain_queue(output_queue, stdout_lines, sys.stdout)
            drain_queue(error_queue, stderr_lines, sys.stderr)

            returncode = process.wait()

        except KeyboardInterrupt:
            log.stop_spinner()
            log.error("CTRL-C pressed, terminate subprocess")
            _terminate(process)
            sys.exit(1)
        except Exception:
            # on_output raised: stop the subprocess rather than leave it running unobserved.
            log.stop_spinner()
            _terminate(process)
            raise

    log.stop_spinner()

    end_timestamp = timer()
    elapsed = timedelta(seconds=end_timestamp - start_timestamp)

    if returncode != 0:
        raise RunError(
            join_command_line(command),
            returncode=returncode,
            stderr=stderr_lines,
        )

    return RunResult(returncode, stdout_lines, stderr_lines, elapsed=elapsed)
