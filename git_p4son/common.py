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

from .log import log


def get_current_branch(workspace_dir: str) -> str | None:
    """Return the current git branch name, or None on error/detached HEAD.

    When in detached HEAD during an interactive rebase, returns the
    original branch name from git's rebase state.
    """
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
            return _get_rebase_branch(workspace_dir)
        return branch
    except Exception:
        return None


def _get_rebase_branch(workspace_dir: str) -> str | None:
    """During interactive rebase, read the original branch from git state."""
    try:
        result = subprocess.run(
            ['git', 'rev-parse', '--git-dir'],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        git_dir = result.stdout.strip()
        head_name_file = os.path.join(git_dir, 'rebase-merge', 'head-name')
        with open(head_name_file) as f:
            ref = f.read().strip()
        prefix = 'refs/heads/'
        if ref.startswith(prefix):
            return ref[len(prefix):]
        return ref
    except FileNotFoundError:
        return None


def get_head_subject(workspace_dir: str) -> str | None:
    """Return the subject line of the HEAD commit, or None on failure."""
    try:
        result = subprocess.run(
            ['git', 'log', '-1', '--format=%s', 'HEAD'],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None
        subject = result.stdout.strip()
        return subject if subject else None
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


def get_p4_client_name(cwd: str) -> str | None:
    """Get the Perforce client name by running p4 info.

    Returns the client name, or None if not in a workspace.
    """
    result = subprocess.run(
        ['p4', 'info'], cwd=cwd,
        capture_output=True, text=True)
    for line in result.stdout.splitlines():
        if line.startswith('Client name:'):
            name = line.split(':', 1)[1].strip()
            if name != '*unknown*':
                return name
    return None


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
        if c.find(' ') != -1:
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
