"""Common utilities shared between git-p4son commands."""

import contextlib
import ntpath
import os
import posixpath
import queue
import re
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from typing import IO, Callable, Generic, Literal, Protocol, TypeVar, overload

from .log import log


def _command_env(cwd: str, extra: dict[str, str] | None) -> dict[str, str]:
    """Return os.environ plus extra, with PWD set to abspath(cwd), which 'p4 add' resolves paths against."""
    return {**os.environ, 'PWD': os.path.abspath(cwd), **(extra or {})}


def _ask(prompt: str, options: list[str], default: str | None,
         shorthands: dict[str, str] | None = None) -> str | None:
    """Ask until the response is an option or its shorthand, by default its first letter; empty picks default.

    Returns None on EOF."""
    if shorthands is None:
        shorthands = {option[0]: option for option in options}
        if len(shorthands) != len(options):
            raise ValueError(f'options need distinct first letters: {options}')
    keys = list(shorthands)
    while True:
        try:
            response = input(prompt).strip().lower()
        except EOFError:
            print()
            return None

        if not response and default is not None:
            return default
        if response in shorthands:
            return shorthands[response]
        if response in options:
            return response
        print('Please enter ' + ', '.join(keys[:-1]) + f' or {keys[-1]}')


def _shorthand(option: str, default: str | None) -> str:
    """The option's letter, capitalized when it is the default."""
    return option[0].upper() if option == default else option[0]


def prompt_choice(question: str, options: list[str],
                  default: str | None = None) -> str | None:
    """Ask for one of options, listed inline, by name or first letter; empty picks default; None on EOF."""
    rendered = ' / '.join(f'[{_shorthand(option, default)}]{option[1:]}'
                          for option in options)
    return _ask(f'{question} {rendered}: ', options, default)


def prompt_menu(question: str, options: list[tuple[str, str]],
                default: str | None = None) -> str | None:
    """Like prompt_choice, but lists each (option, description) on its own line."""
    names = [name for name, _ in options]
    width = max(len(name) for name in names)
    print(question)
    for name, description in options:
        print(f'  [{name[0]}]{name[1:]:<{width + 1}}{description}'.rstrip())
    letters = '/'.join(_shorthand(name, default) for name in names)
    return _ask(f'Choice [{letters}]: ', names, default)


def prompt_numbered(question: str, options: list[str]) -> str | None:
    """Like prompt_menu, but numbers the options, for names that may share a first letter."""
    print(question)
    for number, option in enumerate(options, 1):
        print(f'  [{number}] {option}')
    numbers = {str(number): option for number, option in enumerate(options, 1)}
    return _ask(f'Choice [1-{len(options)}]: ', options, None, numbers)


class _PathModule(Protocol):
    """The parts of ntpath and posixpath that normalize_workspace_path uses."""

    sep: str

    def normpath(self, path: str) -> str: ...
    def normcase(self, s: str) -> str: ...
    def isabs(self, s: str) -> bool: ...
    def commonpath(self, paths: list[str]) -> str: ...
    def relpath(self, path: str, start: str) -> str: ...


def _path_module_for(*paths: str) -> _PathModule:
    """Choose a path module that matches the given path strings."""
    if any('\\' in path or re.match(r'^[A-Za-z]:', path) for path in paths):
        return ntpath
    return posixpath


def normalize_workspace_path(filename: str, workspace_dir: str,
                             allow_outside: bool = False) -> str | None:
    """Return filename as a workspace-relative slash path.

    Returns None for a path outside the workspace, unless allow_outside."""
    pathmod = _path_module_for(filename, workspace_dir)
    normalized_workspace = pathmod.normpath(workspace_dir)
    workspace_case = pathmod.normcase(normalized_workspace)
    normalized_filename = pathmod.normpath(filename)

    if pathmod.isabs(normalized_filename):
        try:
            common = pathmod.commonpath(
                [workspace_case, pathmod.normcase(normalized_filename)])
        except ValueError:
            common = None

        if common == workspace_case:
            normalized_filename = pathmod.relpath(
                normalized_filename, normalized_workspace)
        elif not allow_outside:
            return None

    if not allow_outside and normalized_filename.split(pathmod.sep)[0] == '..':
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


@contextlib.contextmanager
def restoring(restore: Callable[[], None], report: Callable[['RunError'], None]) -> Iterator[None]:
    """Run restore after the body, handing a RunError from it to report, which logs it.

    The body's exception propagates; after a body that succeeded, the RunError does."""
    try:
        yield
    except BaseException:
        try:
            restore()
        except RunError as error:
            report(error)
        raise
    try:
        restore()
    except RunError as error:
        report(error)
        raise


Output = TypeVar('Output', list[str], bytes)


@dataclass
class RunResult(Generic[Output]):
    """A finished command's exit code and output, lines or bytes; elapsed is set by run_with_stream_output."""

    returncode: int
    stdout: Output
    stderr: Output
    elapsed: timedelta | None = None


def join_command_line(command: list[str]) -> str:
    return ''.join(f' "{c}"' if ' ' in c else f' {c}' for c in command)


@overload
def run(command: list[str], cwd: str = '.', dry_run: bool = False,
        input: str | None = None, env: dict[str, str] | None = None,
        text: Literal[True] = True, fail_on_returncode: bool = True
        ) -> RunResult[list[str]]: ...


@overload
def run(command: list[str], cwd: str = '.', dry_run: bool = False,
        input: str | bytes | None = None, env: dict[str, str] | None = None, *,
        text: Literal[False], fail_on_returncode: bool = True
        ) -> RunResult[bytes]: ...


def run(command: list[str], cwd: str = '.', dry_run: bool = False,
        input: str | bytes | None = None,
        env: dict[str, str] | None = None,
        text: bool = True,
        fail_on_returncode: bool = True
        ) -> RunResult[list[str]] | RunResult[bytes]:
    """Run a command and return a RunResult, raising on failure unless fail_on_returncode is False.

    Bytes input, only with text=False, goes to the command as it is."""
    use_spinner = input is None and not dry_run
    log.command(join_command_line(command),
                truncate_for_spinner=use_spinner)

    if dry_run:
        log.end_command()
        return RunResult(0, [], []) if text else RunResult(0, b'', b'')

    if input is not None:
        log.end_command()
        log.stdin(input if isinstance(input, str)
                  else input.decode('utf-8', 'replace'))
    else:
        log.start_spinner()

    command_env = _command_env(cwd, env)

    # Decode as UTF-8: git emits it, but Windows would decode with cp1252.
    try:
        result = subprocess.run(command,
                                cwd=cwd,
                                env=command_env,
                                capture_output=True,
                                text=text,
                                encoding='utf-8' if text else None,
                                errors='replace' if text else None,
                                input=input if text or not isinstance(input, str)
                                else input.encode('utf-8'))
    except OSError:
        # E.g. a missing executable: stop the spinner overwriting the error.
        log.stop_spinner()
        raise

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
                         result.stderr.splitlines())
    return RunResult(result.returncode, result.stdout, result.stderr)


def _read_lines(pipe: IO[str], index: int,
                output_queue: queue.Queue[tuple[int, str | None]]) -> None:
    """Queue (index, line) for each line of pipe, then (index, None) at its end."""
    for line in iter(pipe.readline, ''):
        output_queue.put((index, line.rstrip()))
    output_queue.put((index, None))


def _terminate(process: subprocess.Popen[str]) -> None:
    """Terminate a subprocess, killing it if it does not exit in time."""
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        log.error("Subprocess did not terminate in time. Forcing kill...")
        process.kill()


def run_with_stream_output(command: list[str],
                           on_output: Callable[[str, IO[str]], None],
                           cwd: str = '.',
                           keep_stdout: bool = True) -> RunResult[list[str]]:
    """Run a command, passing each output line and its stream to on_output as it is written.

    An exception raised from on_output terminates the command and propagates. Without keep_stdout,
    the result's stdout is empty; on_output still sees every line."""
    log.command(join_command_line(command),
                truncate_for_spinner=True)
    log.start_spinner()

    start = time.monotonic()

    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    collected = (stdout_lines if keep_stdout else None, stderr_lines)
    streams = (sys.stdout, sys.stderr)

    # UTF-8 as in run(); a decode error would silently kill the reader threads.
    try:
        process = subprocess.Popen(command,
                                   cwd=cwd,
                                   env=_command_env(cwd, None),
                                   stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE,
                                   text=True,
                                   encoding='utf-8',
                                   errors='replace')
    except OSError:
        # E.g. a missing executable: stop the spinner overwriting the error.
        log.stop_spinner()
        raise

    with process:
        output_queue: queue.Queue[tuple[int, str | None]] = queue.Queue()
        for index, pipe in enumerate((process.stdout, process.stderr)):
            threading.Thread(target=_read_lines, args=(pipe, index, output_queue),
                             daemon=True).start()

        try:
            # on_output runs here, not in the readers, so its exceptions and Ctrl-C reach the handlers below.
            open_pipes = 2
            while open_pipes:
                try:
                    # A timeout keeps Ctrl-C responsive on Windows.
                    index, line = output_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    open_pipes -= 1
                    continue
                lines = collected[index]
                if lines is not None:
                    lines.append(line)
                on_output(line, streams[index])

            returncode = process.wait()

        except BaseException:
            # Ctrl-C or on_output raised: stop the subprocess rather than leave it running unobserved.
            log.stop_spinner()
            _terminate(process)
            raise

    log.stop_spinner()

    elapsed = timedelta(seconds=time.monotonic() - start)

    if returncode != 0:
        raise RunError(
            join_command_line(command),
            returncode=returncode,
            stderr=stderr_lines,
        )

    return RunResult(returncode, stdout_lines, stderr_lines, elapsed=elapsed)
