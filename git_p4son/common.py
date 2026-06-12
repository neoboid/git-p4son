"""
Common utilities shared between sync and edit commands.
"""

import ntpath
import os
import posixpath
import queue
import re
import subprocess
import sys
import threading
import time
from timeit import default_timer as timer
from datetime import timedelta
from typing import IO, Callable

from .log import log


def _env_with_pwd(cwd: str) -> dict[str, str]:
    """Return a copy of os.environ with PWD set to abspath(cwd).

    subprocess only changes the child's kernel cwd; PWD is inherited from
    the parent. Tools that read PWD for relative-path resolution (notably
    'p4 add') would otherwise resolve paths against the wrong directory
    when git-p4son is invoked from a subdirectory of the workspace.
    """
    env = os.environ.copy()
    env['PWD'] = os.path.abspath(cwd)
    return env


def branch_to_alias(branch_name: str) -> str:
    """Sanitize a branch name for use as an alias filename."""
    return branch_name.replace('/', '-')


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


def join_command_line(command: list[str]) -> str:
    command_line = ''
    for c in command:
        if ' ' in c:
            command_line += f' "{c}"'
        else:
            command_line += f' {c}'
    return command_line


def run(command: list[str], cwd: str = '.', dry_run: bool = False,
        input: str | None = None,
        env: dict[str, str] | None = None,
        text: bool = True,
        fail_on_returncode: bool = True
        ) -> RunResult:
    """
    Run a command and return the result.

    Args:
        command: List of command arguments
        cwd: Working directory to run the command in
        dry_run: If True, only print the command without executing
        input: Optional string to pass to the subprocess via stdin
        env: Optional environment variables to add or override
        text: False to get raw bytes instead of decoded lines.
        fail_on_returncode: False to not raise on non-zero return codes.

    Returns:
        RunResult object with returncode, stdout, and stderr
    """
    use_spinner = input is None and not dry_run
    log.command(join_command_line(command),
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

    # Decode output as UTF-8 regardless of locale: git emits UTF-8, but
    # Windows would otherwise decode with the ANSI code page (cp1252).
    result = subprocess.run(command,
                            cwd=cwd,
                            env=command_env,
                            capture_output=True,
                            text=text,
                            encoding='utf-8' if text else None,
                            errors='replace' if text else None,
                            input=input)

    end_timestamp = timer()
    elapsed = timedelta(seconds=end_timestamp - start_timestamp)

    log.stop_spinner()

    if fail_on_returncode and result.returncode != 0:
        stderr = result.stderr.splitlines() if text else []
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


def run_with_output(command: list[str], cwd: str = '.',
                    on_output: Callable[..., None] | None = None,
                    env: dict[str, str] | None = None) -> RunResult:
    """
    Run a command with real-time output processing.

    Args:
        command: List of command arguments
        cwd: Working directory to run the command in
        on_output: Callback function for processing output lines
                   If set the funciton will be called with each
                   line and stream (stdout/stderr) as they are written.
        env: Optional environment variables to add or override

    Returns:
        RunResult object with returncode, stdout, and stderr
    """
    log.command(join_command_line(command), truncate_for_spinner=True)
    log.start_spinner()

    start_timestamp = timer()

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    returncode: int | None = None

    command_env = _env_with_pwd(cwd)
    if env:
        command_env.update(env)

    # UTF-8 for the same reason as in run(); a decode error would
    # otherwise kill the reader threads silently.
    with subprocess.Popen(command,
                          cwd=cwd,
                          env=command_env,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,
                          text=True,
                          encoding='utf-8',
                          errors='replace') as process:

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
            # The reader threads own the pipes and exit at EOF, so they are
            # the source of truth for "no more output is coming". Looping on
            # process.poll() instead would race: the process can exit while
            # lines are still in the pipe buffer, and anything enqueued after
            # the last drain would be lost.
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
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                log.error(
                    "Subprocess did not terminate in time. Forcing kill...")
                process.kill()
            sys.exit(1)

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
