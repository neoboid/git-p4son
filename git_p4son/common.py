"""
Common utilities shared between sync and edit commands.
"""

import queue
import subprocess
import sys
import threading
from timeit import default_timer as timer
from datetime import timedelta
from typing import IO, Callable

from .log import log


def branch_to_alias(branch_name: str) -> str:
    """Sanitize a branch name for use as an alias filename."""
    return branch_name.replace('/', '-')


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

    def __init__(self, returncode: int, stdout: list[str], stderr: list[str],
                 elapsed: timedelta | None = None) -> None:
        self.returncode: int = returncode
        self.stdout: list[str] = stdout
        self.stderr: list[str] = stderr
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
        input: str | None = None) -> RunResult:
    """
    Run a command and return the result.

    Args:
        command: List of command arguments
        cwd: Working directory to run the command in
        dry_run: If True, only print the command without executing
        input: Optional string to pass to the subprocess via stdin

    Returns:
        RunResult object with returncode, stdout, and stderr
    """
    log.command(join_command_line(command))

    if dry_run:
        log.end_command()
        return RunResult(0, [], [])

    if input is not None:
        log.end_command()
        log.stdin(input)
    else:
        log.start_spinner()

    start_timestamp = timer()

    result = subprocess.run(command,
                            cwd=cwd,
                            capture_output=True,
                            text=True,
                            input=input)

    end_timestamp = timer()
    elapsed = timedelta(seconds=end_timestamp - start_timestamp)

    log.stop_spinner()

    if result.returncode != 0:
        raise RunError(
            join_command_line(command),
            returncode=result.returncode,
            stderr=result.stderr.splitlines(),
        )

    return RunResult(result.returncode, result.stdout.splitlines(),
                     result.stderr.splitlines(), elapsed=elapsed)


def enqueue_lines(stream: IO[str], output_queue: queue.Queue[str]) -> None:
    """Enqueue lines from a stream into a queue."""
    for line in iter(stream.readline, ''):
        output_queue.put(line.rstrip())


def run_with_output(command: list[str], cwd: str = '.', on_output: Callable[..., None] | None = None) -> RunResult:
    """
    Run a command with real-time output processing.

    Args:
        command: List of command arguments
        cwd: Working directory to run the command in
        on_output: Callback function for processing output lines
                   If set the funciton will be called with each
                   line and stream (stdout/stderr) as they are written.

    Returns:
        RunResult object with returncode, stdout, and stderr
    """
    log.command(join_command_line(command))
    log.start_spinner()

    start_timestamp = timer()

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    returncode: int | None = None

    with subprocess.Popen(command,
                          cwd=cwd,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,
                          text=True) as process:

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
            while True:
                drain_queue(output_queue, stdout_lines, sys.stdout)
                drain_queue(error_queue, stderr_lines, sys.stderr)
                if process.poll() is not None:
                    if output_queue.empty() and error_queue.empty():
                        break

            # Wait for threads to finish
            out_thread.join()
            err_thread.join()

            (final_stdout, final_stderr) = process.communicate()
            returncode = process.returncode

            if final_stdout:
                final_stdout_lines = final_stdout.splitlines()
                stdout_lines = stdout_lines + final_stdout_lines
                for line in final_stdout_lines:
                    if on_output:
                        on_output(line=line, stream=sys.stdout)

            if final_stderr:
                final_stderr_lines = final_stderr.splitlines()
                stderr_lines = stderr_lines + final_stderr_lines
                for line in final_stderr_lines:
                    if on_output:
                        on_output(line=line, stream=sys.stderr)

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
