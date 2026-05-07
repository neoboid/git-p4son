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
                            env=_env_with_pwd(cwd),
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
                          env=_env_with_pwd(cwd),
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
