"""Structured output: all user-facing output goes through the `log` singleton."""

import shutil
import sys
import threading
from datetime import timedelta

_HEADING_PREFIX = '#'

_GREEN = '\033[32m'
_YELLOW = '\033[33m'
_CYAN = '\033[36m'
_MAGENTA = '\033[35m'
_RED = '\033[31m'
_ORANGE = '\033[38;5;208m'
_RESET = '\033[0m'
_CLEAR_TO_EOL = '\033[K'


class Color:
    """Semantic color assignments."""
    HEADING = _MAGENTA
    COMMAND = _CYAN
    SUCCESS = _GREEN
    ERROR = _RED
    WARNING = _ORANGE
    ADD = _GREEN
    DELETE = _RED
    MODIFY = _YELLOW
    UNTRACKED = _ORANGE


def _is_tty(stream) -> bool:
    """Return True if the stream is an interactive terminal."""
    return hasattr(stream, 'isatty') and stream.isatty()


def _color(text: str, color: str, stream) -> str:
    """Wrap text in ANSI color codes if the stream supports color."""
    if _is_tty(stream):
        return f'{color}{text}{_RESET}'
    return text


def _color_status(status: str, color: str, stream) -> str:
    """Format status, colored if the stream supports color."""
    colored_status = _color(status, color, stream)
    return f'[{colored_status}]'


def _truncate_to_terminal_width(line: str) -> str:
    """Return line shortened to one terminal row if needed."""
    width = shutil.get_terminal_size(fallback=(120, 20)).columns
    # Leave room for the spinner character appended by _spin.
    max_len = max(20, width - 2)
    if len(line) <= max_len:
        return line

    placeholder = ' ... '
    keep = max_len - len(placeholder)
    prefix_len = keep // 2
    suffix_len = keep - prefix_len
    return f'{line[:prefix_len]}{placeholder}{line[-suffix_len:]}'


_SPINNER_CHARS = '|/-\\'
_SPINNER_INTERVAL = 0.1  # seconds between frames


class Log:
    """Structured output handler for git-p4son."""

    def __init__(self) -> None:
        self.verbose_mode: bool = False
        # Silences stdout, which shell completion reserves for candidates.
        self.quiet_mode: bool = False
        self._heading_count: int = 0
        self._command_line_open: bool = False
        self._spinner_thread: threading.Thread | None = None
        self._spinner_stop: threading.Event = threading.Event()
        self._spinner_line: str = ''
        self._spinner_final_line: str = ''

    def heading(self, text: str) -> None:
        """Print a section heading."""
        if self.quiet_mode:
            return
        if self._heading_count > 0:
            print()
        self._heading_count += 1
        line = f'{_HEADING_PREFIX} {text}'
        print(_color(line, Color.HEADING, sys.stdout))

    def success(self, text: str) -> None:
        """Print a success message to stdout."""
        if self.quiet_mode:
            return
        ok = _color_status('ok', Color.SUCCESS, sys.stdout)
        print(f'{ok} {text}', file=sys.stdout)

    def warning(self, text: str) -> None:
        """Print a warning message to stdout."""
        if self.quiet_mode:
            return
        warn = _color_status('warn', Color.WARNING, sys.stdout)
        print(f'{warn} {text}', file=sys.stdout)

    def error(self, text: str) -> None:
        """Print an error message to stderr."""
        error = _color_status('err', Color.ERROR, sys.stderr)
        print(f'{error} {text}', file=sys.stderr)

    def command(self, cmd: str, truncate_for_spinner: bool = False) -> None:
        """Print a subprocess command line."""
        if self.quiet_mode:
            return
        full_line = f'> {cmd}'
        if not _is_tty(sys.stdout):
            # No terminal: print the full line once, without a spinner.
            print(self._format_command_line(full_line))
            self._command_line_open = False
            return

        line = full_line
        if truncate_for_spinner:
            line = _truncate_to_terminal_width(full_line)

        print(self._format_command_line(line), end='', flush=True)
        self._command_line_open = True
        self._spinner_line = line
        self._spinner_final_line = full_line

    def end_command(self) -> None:
        """Finish the command line (print newline)."""
        if self.quiet_mode:
            return
        if self._command_line_open:
            print()
            self._command_line_open = False

    def info(self, text: str) -> None:
        """Print an informational status line."""
        if self.quiet_mode:
            return
        print(text)

    def output(self, line: str, stderr: bool = False) -> None:
        """Print a line of a child process's output as is."""
        if self.quiet_mode:
            return
        print(line, file=sys.stderr if stderr else sys.stdout)

    def stdin(self, text: str) -> None:
        """Print stdin input sent to a command (verbose only)."""
        if not self.verbose_mode or self.quiet_mode:
            return
        print('stdin:')
        for line in text.splitlines():
            print(f'  {line}')

    def elapsed(self, duration: timedelta) -> None:
        """Print elapsed time."""
        if self.quiet_mode:
            return
        print(f'elapsed: {duration}')

    def file_change(self, filename: str, change: str) -> None:
        """Print a file change line ('add', 'delete', 'modify', 'untracked') with colored prefix."""
        if self.quiet_mode:
            return
        prefixes = {
            'add': ('+', Color.ADD),
            'delete': ('-', Color.DELETE),
            'modify': ('~', Color.MODIFY),
            'untracked': ('?', Color.UNTRACKED),
        }
        symbol, color = prefixes.get(change, ('?', _RESET))
        prefix = _color(symbol, color, sys.stdout)
        print(f'  {prefix} {filename}')

    def start_spinner(self) -> None:
        """Start the spinner at the end of the current command line."""
        if self.quiet_mode or not _is_tty(sys.stdout):
            return
        self._spinner_stop.clear()
        self._spinner_thread = threading.Thread(
            target=self._spin, daemon=True)
        self._spinner_thread.start()

    def stop_spinner(self) -> None:
        """Stop the spinner and reprint the clean command line."""
        if self._spinner_thread is None:
            # No spinner ran, but the command line may still need its newline.
            self.end_command()
            return
        self._spinner_stop.set()
        self._spinner_thread.join()
        self._spinner_thread = None
        # Clear the spinner line, then print the full command once.
        sys.stdout.write(f'\r{_CLEAR_TO_EOL}')
        line = self._format_command_line(self._spinner_final_line)
        sys.stdout.write(f'{line}{_CLEAR_TO_EOL}')
        sys.stdout.write('\n')
        sys.stdout.flush()
        self._command_line_open = False

    def _format_command_line(self, line: str) -> str:
        """Return a command line with a colored prompt when supported."""
        if _is_tty(sys.stdout) and line.startswith('> '):
            return f'{Color.COMMAND}>{_RESET} {line[2:]}'
        return line

    def _spin(self) -> None:
        """Background thread: animate the spinner."""
        idx = 0
        line = self._spinner_line
        while not self._spinner_stop.wait(_SPINNER_INTERVAL):
            char = _SPINNER_CHARS[idx % len(_SPINNER_CHARS)]
            sys.stdout.write(f'\r{line} {char}')
            sys.stdout.flush()
            idx += 1


log = Log()
