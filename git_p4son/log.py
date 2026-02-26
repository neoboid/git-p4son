"""
Structured output module for git-p4son.

All user-facing output goes through the module-level `log` singleton.
This centralises formatting, verbosity filtering, and future color support.
"""

import sys
import threading
from datetime import timedelta

# Heading prefix — single constant, easy to change later.
HEADING_PREFIX = '#'

# ANSI color codes.
_GREEN = '\033[32m'
_YELLOW = '\033[33m'
_CYAN = '\033[36m'
_RED = '\033[31m'
_ORANGE = '\033[38;5;208m'
_RESET = '\033[0m'
_CLEAR_TO_EOL = '\033[K'


class Color:
    """Semantic color assignments."""
    HEADING = _YELLOW
    COMMAND = _CYAN
    SUCCESS = _GREEN
    ERROR = _RED
    WARNING = _ORANGE
    FAIL = _RED
    ADD = _GREEN
    DELETE = _RED
    MODIFY = _YELLOW
    UNTRACKED = _ORANGE
    RESET = _RESET


def _use_color(stream) -> bool:
    """Return True if the stream is a TTY and supports color."""
    return hasattr(stream, 'isatty') and stream.isatty()


def _color(text: str, color: str, stream) -> str:
    """Wrap text in ANSI color codes if the stream supports color."""
    if _use_color(stream):
        return f'{color}{text}{_RESET}'
    return text

def _color_status(status: str, color: str, stream) -> str:
    """Format and wrap status in ANSI color codes
       if the stream supports color."""
    colored_status = _color(status, Color.SUCCESS, sys.stdout)
    return f'[{ok}]'


# Spinner characters — simple ASCII set.
_SPINNER_CHARS = '|/-\\'
_SPINNER_INTERVAL = 0.1  # seconds between frames


class Log:
    """Structured output handler for git-p4son."""

    def __init__(self) -> None:
        self.verbose_mode: bool = False
        self._heading_count: int = 0
        self._spinner_thread: threading.Thread | None = None
        self._spinner_stop: threading.Event = threading.Event()
        self._spinner_line: str = ''

    def heading(self, text: str) -> None:
        """Print a section heading."""
        if self._heading_count > 0:
            print()
        self._heading_count += 1
        line = f'{HEADING_PREFIX} {text}'
        print(_color(line, Color.HEADING, sys.stdout))

    def success(self, text: str) -> None:
        """Print an success message to stdout."""
        ok = _color_status('ok', Color.SUCCESS, sys.stdout)
        print(f'{ok} {text}', file=sys.stdout)

    def warning(self, text: str) -> None:
        """Print an warning message to stdout."""
        warn = _color_status('warn', Color.WARNING, sys.stdout)
        print(f'{warn} {text}', file=sys.stdout)

    def error(self, text: str) -> None:
        """Print an error message to stderr."""
        error = _color_status('err', Color.ERROR, sys.stderr)
        print(f'{error} {text}', file=sys.stderr)

    def command(self, cmd: str) -> None:
        """Print a subprocess command line."""
        prompt = _color('>', Color.COMMAND, sys.stdout)
        print(f'{prompt} {cmd}', end='', flush=True)
        self._spinner_line = f'> {cmd}'

    def end_command(self) -> None:
        """Finish the command line (print newline)."""
        print()

    def detail(self, key: str, value: object) -> None:
        """Print a key-value result."""
        print(f'{key}: {value}')

    def info(self, text: str) -> None:
        """Print an informational status line."""
        print(text)

    def verbose(self, text: str) -> None:
        """Print verbose-only text (suppressed at normal verbosity)."""
        if self.verbose_mode:
            print(text)

    def stdin(self, text: str) -> None:
        """Print stdin input sent to a command (verbose only)."""
        if not self.verbose_mode:
            return
        print('stdin:')
        for line in text.splitlines():
            print(f'  {line}')

    def elapsed(self, duration: timedelta) -> None:
        """Print elapsed time."""
        print(f'elapsed: {duration}')

    def file_change(self, filename: str, change: str) -> None:
        """Print a file change line with colored prefix.

        change is one of 'add', 'delete', 'modify', 'untracked'.
        """
        prefixes = {
            'add': ('+', Color.ADD),
            'delete': ('-', Color.DELETE),
            'modify': ('~', Color.MODIFY),
            'untracked': ('?', Color.UNTRACKED),
        }
        symbol, color = prefixes.get(change, ('?', _RESET))
        prefix = _color(symbol, color, sys.stdout)
        print(f'  {prefix} {filename}')

    def fail(self, returncode: int) -> None:
        """Print a failure message with return code to stderr."""
        failed = _color('Failed', Color.FAIL, sys.stderr)
        print(f'{failed} with return code {returncode}', file=sys.stderr)

    def start_spinner(self) -> None:
        """Start the spinner at the end of the current command line."""
        self._spinner_stop.clear()
        self._spinner_thread = threading.Thread(
            target=self._spin, daemon=True)
        self._spinner_thread.start()

    def stop_spinner(self) -> None:
        """Stop the spinner and reprint the clean command line."""
        if self._spinner_thread is None:
            return
        self._spinner_stop.set()
        self._spinner_thread.join()
        self._spinner_thread = None
        # Clear the spinner character by reprinting the line with color
        line = self._spinner_line
        if _use_color(sys.stdout) and line.startswith('> '):
            line = f'{Color.COMMAND}>{_RESET} {line[2:]}'
        sys.stdout.write(f'\r{line}{_CLEAR_TO_EOL}')
        sys.stdout.write('\n')
        sys.stdout.flush()

    def _spin(self) -> None:
        """Background thread: animate the spinner."""
        idx = 0
        line = self._spinner_line
        while not self._spinner_stop.wait(_SPINNER_INTERVAL):
            char = _SPINNER_CHARS[idx % len(_SPINNER_CHARS)]
            sys.stdout.write(f'\r{line} {char}')
            sys.stdout.flush()
            idx += 1


# Module-level singleton, imported everywhere.
log = Log()
