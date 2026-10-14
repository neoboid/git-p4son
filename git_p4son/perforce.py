"""Perforce abstraction layer: all direct p4 CLI calls live here."""

import re
import sys
from dataclasses import dataclass
from enum import Enum
from typing import IO, Callable, TypeVar

from .common import (
    CommandError,
    RunError,
    RunResult,
    normalize_workspace_path,
    run,
    run_with_stream_output,
)
from .log import log


# The output parsing matches p4's English messages, so ask for those whatever P4LANGUAGE says.
_P4 = ['p4', '-L', 'en']


# --- ztag output parsing ---

def parse_ztag_output(lines: list[str]) -> dict[str, str]:
    """Parse p4 -ztag output into a dict."""
    fields = {}
    for line in lines:
        if line.startswith('... '):
            key, _, value = line[4:].partition(' ')
            fields[key] = value
    return fields


def parse_ztag_multi_output(lines: list[str]) -> list[dict[str, str]]:
    """Parse p4 -ztag output with multiple records into a list of dicts."""
    records = []
    current: dict[str, str] = {}
    last_key: str | None = None
    for line in lines:
        if line.startswith('... '):
            key, _, value = line[4:].partition(' ')
            current[key] = value
            last_key = key
        elif not line.strip():
            if current:
                records.append(current)
                current = {}
            last_key = None
        elif last_key is not None:
            current[last_key] += f'\n{line}'
    if current:
        records.append(current)
    return records


# --- client spec ---

class ClobberMode(Enum):
    """How p4 sync treats writable files in the workspace."""

    NOCLOBBER = 'noclobber'
    CLOBBER = 'clobber'
    ALLWRITE = 'allwrite'


@dataclass
class P4ClientSpec:
    """Parsed Perforce client specification."""

    name: str
    root: str
    options: list[str]
    line_end: str

    @property
    def allwrite(self) -> bool:
        return 'allwrite' in self.options

    @property
    def clobber_mode(self) -> ClobberMode:
        # clobber overwrites writable files regardless, so it wins over allwrite.
        if 'clobber' in self.options:
            return ClobberMode.CLOBBER
        if self.allwrite:
            return ClobberMode.ALLWRITE
        return ClobberMode.NOCLOBBER

    @property
    def uses_crlf(self) -> bool:
        return self.line_end == 'win' or (self.line_end == 'local'
                                          and sys.platform == 'win32')


def get_client_spec(cwd: str) -> P4ClientSpec | None:
    """Get the client spec, or None if not in a valid workspace."""
    result = run([*_P4, '-ztag', 'client', '-o'], cwd=cwd)
    fields = parse_ztag_output(result.stdout)
    if 'Update' not in fields:
        return None
    return P4ClientSpec(
        name=fields['Client'],
        root=fields['Root'],
        options=fields['Options'].split(),
        line_end=fields.get('LineEnd', 'local'),
    )


# --- changelist spec parsing ---

def find_line_starting_with(lines: list[str], prefix: str) -> int:
    """Find the index of the first line starting with prefix, or len(lines) if not found."""
    return next((i for i, line in enumerate(lines) if line.startswith(prefix)),
                len(lines))


def find_end_of_indented_section(lines: list[str], start: int) -> int:
    """Find the end of a tab-indented section starting at the given index."""
    i = start
    while i < len(lines) and lines[i].startswith('\t'):
        i += 1
    return i


def extract_description_lines(spec_text: str) -> list[str]:
    """Extract the Description field lines from a p4 changelist spec."""
    lines = spec_text.splitlines()
    start = find_line_starting_with(lines, 'Description:') + 1
    end = find_end_of_indented_section(lines, start)
    return [line[1:] for line in lines[start:end]]


def replace_description_in_spec(spec_text: str, new_description_lines: list[str]) -> str:
    """Replace the Description field in a p4 changelist spec."""
    lines = spec_text.splitlines()
    desc_line = find_line_starting_with(lines, 'Description:')

    if desc_line >= len(lines):
        return spec_text

    desc_end = find_end_of_indented_section(lines, desc_line + 1)

    result_lines = lines[:desc_line + 1]
    result_lines.extend('\t' + line for line in new_description_lines)
    result_lines.extend(lines[desc_end:])

    return '\n'.join(result_lines) + '\n'


# --- changelist operations ---

def get_changelist_spec(changelist_nr: str, workspace_dir: str) -> str:
    """Fetch the changelist spec from Perforce."""
    result = run([*_P4, 'change', '-o', changelist_nr], cwd=workspace_dir)
    return '\n'.join(result.stdout) + '\n'


def save_changelist_spec(spec_text: str, workspace_dir: str) -> None:
    """Write a changelist spec back to Perforce."""
    run([*_P4, 'change', '-i'], cwd=workspace_dir, input=spec_text)


def p4_create_changelist(description_lines: list[str],
                         workspace_dir: str) -> str:
    """Create a pending changelist with the given description and return its number."""
    tabbed_description = '\n\t'.join(description_lines)
    spec = f'Change: new\n\nDescription:\n\t{tabbed_description}\n'
    result = run([*_P4, 'change', '-i'], cwd=workspace_dir, input=spec)
    for line in result.stdout:
        match = re.search(r'Change (\d+) created', line)
        if match:
            return match.group(1)
    raise CommandError(
        'Failed to extract changelist number from p4 change output')


def p4_depot_root_exists(depot_root: str, workspace_dir: str) -> bool:
    """Report whether p4 accepts the depot root as a path to query changelists for."""
    try:
        run([*_P4, 'changes', '-m1', '-s', 'submitted', f'{depot_root}/...'],
            cwd=workspace_dir)
        return True
    except CommandError:
        return False


def get_latest_changelist(depot_root: str, workspace_dir: str) -> int:
    """Get the latest submitted changelist affecting the depot root."""
    res = run([*_P4, '-ztag', 'changes', '-m1', '-s', 'submitted',
              f'{depot_root}/...#head'], cwd=workspace_dir)
    fields = parse_ztag_output(res.stdout)
    if 'change' not in fields:
        raise CommandError('No changelists found affecting workspace')
    return int(fields['change'])


@dataclass
class P4Change:
    """A submitted changelist affecting the depot root."""

    change: int
    user: str


def get_submitted_changes(depot_root: str, start: int, end: int,
                          workspace_dir: str) -> list[P4Change]:
    """List changelists submitted to the depot root in [start, end], oldest first."""
    res = run([*_P4, '-ztag', 'changes', '-s', 'submitted',
               f'{depot_root}/...@{start},@{end}'], cwd=workspace_dir)
    records = parse_ztag_multi_output(res.stdout)
    changes = [P4Change(change=int(r['change']), user=r.get('user', ''))
               for r in records if 'change' in r]
    return sorted(changes, key=lambda c: c.change)


def get_p4_user(workspace_dir: str) -> str | None:
    """Get the Perforce user name of the current connection, or None if the server does not know it."""
    res = run([*_P4, '-ztag', 'info'], cwd=workspace_dir)
    user = parse_ztag_output(res.stdout).get('userName')
    # The server reports a user it has no spec for as *unknown*.
    return None if user in (None, '', '*unknown*') else user


def get_existing_p4_users(names: list[str], workspace_dir: str) -> list[str]:
    """Return the names that are Perforce users, spelled as the server spells them."""
    res = run([*_P4, '-ztag', 'users', *names], cwd=workspace_dir,
              fail_on_returncode=False)
    errors = [line for line in res.stderr
              if line.strip() and 'no such user' not in line]
    if res.returncode != 0 and errors:
        raise RunError('p4 users failed', res.returncode, errors)
    return [record['User'] for record in parse_ztag_multi_output(res.stdout)
            if 'User' in record]


# --- file operations ---

def _warn_p4_errors(res: RunResult, expected: tuple[str, ...],
                    summary: str) -> None:
    """Warn with the stderr lines of a failed p4 command that match none of the expected messages."""
    errors = [line for line in res.stderr
              if not any(message in line for message in expected)]
    if res.returncode != 0 and errors:
        log.warning(summary)
        for line in errors:
            log.info(f'  {line}')


def _fstat_records(res: RunResult, workspace_dir: str,
                   allow_outside: bool = False) -> list[tuple[str, dict[str, str]]]:
    """Pair each fstat record with its workspace-relative path, skipping records without one."""
    records = []
    for record in parse_ztag_multi_output(res.stdout):
        path = (record.get('path') or record.get('clientFile')
                or record.get('depotFile'))
        if not path:
            continue
        filename = normalize_workspace_path(path, workspace_dir, allow_outside)
        if filename is not None:
            records.append((filename, record))
    return records


def get_opened_files(filenames: list[str],
                     workspace_dir: str) -> dict[str, tuple[str, str]]:
    """Return (changelist, action) for each of the files that is opened, keyed by workspace-relative path."""
    if not filenames:
        return {}
    res = run([*_P4, '-x', '-', '-ztag', 'fstat', '-Ro', '-Op',
               '-T', 'path,clientFile,action,change'],
              cwd=workspace_dir, input='\n'.join(filenames),
              fail_on_returncode=False)
    # Files that are not opened, or not in the depot yet, are reported per file.
    _warn_p4_errors(res, ('not opened', 'no such file'),
                    'p4 fstat failed to list opened files:')
    return {filename: (record.get('change', ''), record.get('action', ''))
            for filename, record in _fstat_records(res, workspace_dir)}


_State = TypeVar('_State')


def opened_lookup(opened: dict[str, _State]) -> Callable[[str], _State | None]:
    """Look files up in a map of opened files, ignoring case where there is no exact match."""
    # p4 may spell a path in the client's case on case-insensitive file systems.
    folded = {path.casefold(): state for path, state in opened.items()}
    return lambda filename: (opened.get(filename)
                             or folded.get(filename.casefold()))


def run_p4_batch(p4_args: list[str], filenames: list[str], summary: str,
                 workspace_dir: str, dry_run: bool,
                 fail_on_returncode: bool = True) -> list[str]:
    """List filenames under summary, then run one p4 command over them all; returns p4's output lines."""
    if not filenames:
        return []
    log.info(f'{summary}:')
    for filename in filenames:
        log.info(f'  {filename}')
    result = run([*_P4, '-x', '-', *p4_args], cwd=workspace_dir,
                 input='\n'.join(filenames), dry_run=dry_run,
                 fail_on_returncode=fail_on_returncode)
    return result.stdout + result.stderr


def get_opened_files_in_changelist(changelist: str,
                                   workspace_dir: str) -> list[tuple[str, str]]:
    """Return (path, action) for files opened in a changelist, as workspace-relative paths."""
    res = run([*_P4, '-ztag', 'fstat', '-Ro', '-e', changelist, '-Op',
               '-T', 'path,clientFile,action', '...'],
              cwd=workspace_dir, fail_on_returncode=False)
    _warn_p4_errors(res, ('not opened',),
                    'p4 fstat failed to list opened files:')
    return [(filename, record.get('action', ''))
            for filename, record in _fstat_records(res, workspace_dir)]


def p4_revert(filenames: list[str], workspace_dir: str,
              dry_run: bool) -> None:
    """Revert the given files, writing their depot version to disk."""
    run([*_P4, '-x', '-', 'revert'], cwd=workspace_dir,
        input='\n'.join(filenames), dry_run=dry_run)


def p4_revert_unchanged(filenames: list[str], changelist: str,
                        workspace_dir: str, dry_run: bool) -> list[str]:
    """Revert files that are unchanged, or missing and opened for add; returns p4's lines."""
    if not filenames:
        return []
    args = [*_P4, '-x', '-', 'revert', '-a']
    if dry_run:
        args.append('-n')
    args += ['-c', changelist]
    # revert -a exits non-zero when none of the files qualify.
    result = run(args, cwd=workspace_dir, input='\n'.join(filenames),
                 fail_on_returncode=False)
    return [line for line in result.stdout if 'reverted' in line]


_ACTION_TO_CHANGE = {'add': 'add', 'move/add': 'add',
                     'delete': 'delete', 'move/delete': 'delete'}


def _p4_action_to_change(action: str) -> str:
    """Convert a p4 action to a git-p4son change label."""
    return _ACTION_TO_CHANGE.get(action, 'modify')


def p4_get_opened_files(depot_root: str,
                        workspace_dir: str) -> list[tuple[str, str]]:
    """Return client paths and change types for files opened in Perforce."""
    res = run(
        [*_P4, '-ztag', 'fstat', '-Ro', '-Op', '-T',
         'depotFile,path,clientFile,action', f'{depot_root}/...'],
        cwd=workspace_dir)
    return [(filename, _p4_action_to_change(record['action']))
            for filename, record in _fstat_records(res, workspace_dir,
                                                   allow_outside=True)]


# --- shelving ---

def p4_shelve_changelist(changelist: str, workspace_dir: str, dry_run: bool) -> None:
    """Shelve a changelist's open files, replacing the shelf and leaving unchanged files out."""
    run([*_P4, 'shelve', '-r', '-a', 'leaveunchanged', '-Af',
         '-c', changelist],
        cwd=workspace_dir, dry_run=dry_run)


# --- sync ---

_CLOBBER_PREFIX = "Can't clobber writable file "
_SYNC_LINE_PATTERNS = (
    ('add', ' - added as '),
    ('del', ' - deleted as '),
    ('upd', ' - updating '),
    ('clb', _CLOBBER_PREFIX),
)
_UP_TO_DATE_RE = re.compile(r"@\d+ - file\(s\) up-to-date\.")


def get_writable_files(stderr_lines: list[str]) -> list[str]:
    """Extract writable files from p4 sync stderr output."""
    return [line[len(_CLOBBER_PREFIX):].rstrip()
            for line in stderr_lines if line.startswith(_CLOBBER_PREFIX)]


def parse_p4_sync_line(line: str) -> tuple[str | None, str | None]:
    """Return the (mode, path) of a p4 sync output line, or (None, None) for any other line."""
    for mode, pattern in _SYNC_LINE_PATTERNS:
        tokens = line.split(pattern)
        if len(tokens) == 2:
            return (mode, tokens[1])

    return (None, None)


# On Windows p4's rename into place fails while another process holds the file open.
_RENAME_FAILED_PREFIX = 'rename: failed to rename '


class P4SyncAbortError(CommandError):
    """Raised to stop a p4 sync that can no longer succeed; the message is p4's output line."""


class P4SyncOutputProcessor:
    """Process p4 sync output in real-time."""

    def __init__(self) -> None:
        self.stats: dict[str, int] = dict.fromkeys(
            [mode for mode, _ in _SYNC_LINE_PATTERNS], 0)

    def __call__(self, line: str, stream: IO[str]) -> None:
        if _UP_TO_DATE_RE.search(line):
            log.info('all files up to date')
            return

        if line.startswith(_RENAME_FAILED_PREFIX):
            raise P4SyncAbortError(line)

        mode, filename = parse_p4_sync_line(line)
        if not mode or not filename:
            log.warning(f'Unparsable line: {line}')
            return

        self.stats[mode] += 1

    def get_summary(self) -> str:
        """Get a one-line sync summary."""
        synced_count = self.stats['add'] + \
            self.stats['upd'] - self.stats['clb']
        detail = ', '.join(f'{mode}: {self.stats[mode]}'
                           for mode in ('add', 'upd', 'del', 'clb')
                           if self.stats[mode])
        return f'synced {synced_count} files' + (f' ({detail})' if detail else '')


def p4_head_types(filenames: list[str], workspace_dir: str) -> dict[str, str]:
    """Map local paths to their Perforce head file type; files not found are omitted."""
    if not filenames:
        return {}
    # Use -x - so the path list doesn't hit the command-line length limit.
    args = [*_P4, '-x', '-', '-ztag', 'fstat',
            '-T', 'clientFile,headType']
    result = run(args, cwd=workspace_dir, input='\n'.join(filenames))
    return {record['clientFile']: record['headType']
            for record in parse_ztag_multi_output(result.stdout)
            if record.get('clientFile') and record.get('headType')}


def is_binary_file_type(head_type: str) -> bool:
    """Check if a Perforce headType indicates a binary file."""
    base_type = head_type.split('+')[0]
    return base_type in ('binary', 'ubinary')


def is_always_writable_file_type(head_type: str) -> bool:
    """Check if a Perforce headType carries the +w (always writable) modifier."""
    _, _, modifiers = head_type.partition('+')
    return 'w' in modifiers


@dataclass
class P4SyncPreviewFile:
    """A file a sync would affect; mode is 'add', 'upd' or 'del' relative to the have-list."""
    mode: str
    filepath: str


def _run_p4_sync(args: list[str], workspace_dir: str,
                 processor: P4SyncOutputProcessor,
                 keep_stdout: bool = True) -> RunResult:
    """Run a p4 sync with processor watching its output, then log how long it took."""
    result = run_with_stream_output([*_P4, 'sync', *args], cwd=workspace_dir,
                                    on_output=processor, keep_stdout=keep_stdout)
    if result.elapsed:
        log.elapsed(result.elapsed)
    return result


def p4_sync_preview(changelist: int, depot_root: str,
                    workspace_dir: str) -> list[P4SyncPreviewFile]:
    """Preview which files would be synced without actually syncing."""
    log.heading(f'Previewing sync to CL {changelist}')
    result = _run_p4_sync(['-n', f'{depot_root}/...@{changelist}'],
                          workspace_dir, P4SyncOutputProcessor())

    files = []
    for line in result.stdout:
        mode, filename = parse_p4_sync_line(line)
        if mode and filename:
            files.append(P4SyncPreviewFile(mode=mode, filepath=filename))
    log.success(f'{len(files)} files would be synced')
    return files


class P4SyncClobberError(RunError):
    """p4 sync refused to overwrite writable files; refused lists them."""

    def __init__(self, error: RunError, refused: list[str]) -> None:
        super().__init__(str(error), error.returncode, error.stderr)
        self.refused = refused


def p4_sync(changelist: int, depot_root: str, workspace_dir: str) -> None:
    """Sync the depot root to changelist.

    Raises P4SyncClobberError when p4 refused to overwrite writable files.
    """
    output_processor = P4SyncOutputProcessor()
    try:
        # The processor reads every line as it arrives; holding a large sync's output would only cost memory.
        _run_p4_sync([f'{depot_root}/...@{changelist}'], workspace_dir,
                     output_processor, keep_stdout=False)
    except RunError as e:
        refused = get_writable_files(e.stderr)
        if not refused:
            raise
        log.info(output_processor.get_summary())
        raise P4SyncClobberError(e, refused) from e
    log.success(output_processor.get_summary())
