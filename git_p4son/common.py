"""
Common utilities shared between sync and edit commands.
"""

import os
import os.path
import queue
import subprocess
import sys
import threading
from timeit import default_timer as timer
from datetime import timedelta
from typing import IO, Callable


def get_current_branch(workspace_dir: str) -> str | None:
    """Return the current git branch name, or None on error/detached HEAD."""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        branch = result.stdout.strip()
        if branch == 'HEAD':
            return None
        return branch
    except Exception:
        return None


def branch_to_alias(branch_name: str) -> str:
    """Sanitize a branch name for use as an alias filename."""
    return branch_name.replace('/', '-')


def is_workspace_dir(directory: str) -> bool:
    """Check if a directory is a git workspace."""
    return os.path.isdir(os.path.join(directory, '.git'))


def get_workspace_dir() -> str | None:
    """Find the git workspace root directory by walking up the directory tree."""
    candidate_dir = os.getcwd()
    while True:
        if is_workspace_dir(candidate_dir):
            return candidate_dir

        parent_dir = os.path.dirname(candidate_dir)
        if parent_dir == candidate_dir:
            return None
        candidate_dir = parent_dir


def ensure_workspace() -> str:
    """Ensure we're in a git workspace and return the workspace directory."""
    workspace_dir = get_workspace_dir()
    if not workspace_dir:
        print('Failed to find workspace root directory', file=sys.stderr)
        sys.exit(1)
    return workspace_dir


class RunResult:
    """Result of a command execution."""

    def __init__(self, returncode: int, stdout: list[str], stderr: list[str]) -> None:
        self.returncode: int = returncode
        self.stdout: list[str] = stdout
        self.stderr: list[str] = stderr


def join_command_line(command: list[str]) -> str:
    command_line = ''
    for c in command:
        if c.find(' ') != -1:
            command_line += ' "%s"' % c
        else:
            command_line += ' %s' % c
    return command_line


def run(command: list[str], cwd: str = '.', dry_run: bool = False) -> RunResult:
    """
    Run a command and return the result.

    Args:
        command: List of command arguments
        cwd: Working directory to run the command in
        dry_run: If True, only print the command without executing

    Returns:
        RunResult object with returncode, stdout, and stderr
    """
    print('>', join_command_line(command))

    if dry_run:
        return RunResult(0, [], [])

    start_timestamp = timer()

    result = subprocess.run(command,
                            cwd=cwd,
                            capture_output=True,
                            text=True)

    end_timestamp = timer()

    print('Elapsed time is', timedelta(seconds=end_timestamp - start_timestamp))

    return RunResult(result.returncode, result.stdout.splitlines(), result.stderr.splitlines())


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
    print('>', join_command_line(command))

    start_timestamp = timer()

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    returncode: int | None = None

    with subprocess.Popen(command,
                          cwd=cwd,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,
                          stdin=None,
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

        def poll_queue_until_empty(q, lines, cb):
            try:
                while not q.empty():
                    line = q.get_nowait()
                    lines.append(line)
                    if cb:
                        cb(line)
            except queue.Empty:
                pass
        try:
            def on_stdout(l): return on_output(
                line=l, stream=sys.stdout) if on_output else None

            def on_stderr(l): return on_output(
                line=l, stream=sys.stderr) if on_output else None
            while True:
                poll_queue_until_empty(output_queue,
                                       stdout_lines,
                                       on_stdout)
                poll_queue_until_empty(error_queue,
                                       stderr_lines,
                                       on_stderr)
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
                for l in final_stdout_lines:
                    on_stdout(l)

            if final_stderr:
                final_stderr_lines = final_stderr.splitlines()
                stderr_lines = stderr_lines + final_stderr_lines
                for l in final_stderr_lines:
                    on_stderr(l)

        except KeyboardInterrupt:
            print("CTRL-C pressed, terminate subprocess")
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                print("Subprocess did not terminate in time. Forcing kill...")
                process.kill()
            sys.exit(1)

    end_timestamp = timer()

    print('Elapsed time is', timedelta(seconds=end_timestamp - start_timestamp))

    return RunResult(returncode, stdout_lines, stderr_lines)
