"""Perforce abstraction layer: all direct p4 CLI calls live here."""

import re
import sys
from dataclasses import dataclass
from enum import Enum
from typing import IO, Callable

from .common import (
    CommandError,
    RunError,
    RunResult,
    normalize_workspace_path,
    run,
    run_with_output,
)
from .git import LocalChanges, restore_files
from .log import log


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
    result = run(['p4', '-ztag', 'client', '-o'], cwd=cwd)
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
    result = run(['p4', 'change', '-o', changelist_nr], cwd=workspace_dir)
    return '\n'.join(result.stdout) + '\n'


def save_changelist_spec(spec_text: str, workspace_dir: str) -> None:
    """Write a changelist spec back to Perforce."""
    run(['p4', 'change', '-i'], cwd=workspace_dir, input=spec_text)


def p4_create_changelist(description_lines: list[str],
                         workspace_dir: str) -> str:
    """Create a pending changelist with the given description and return its number."""
    tabbed_description = '\n\t'.join(description_lines)
    spec = f'Change: new\n\nDescription:\n\t{tabbed_description}\n'
    result = run(['p4', 'change', '-i'], cwd=workspace_dir, input=spec)
    for line in result.stdout:
        match = re.search(r'Change (\d+) created', line)
        if match:
            return match.group(1)
    raise CommandError(
        'Failed to extract changelist number from p4 change output')


def p4_depot_root_exists(depot_root: str, workspace_dir: str) -> bool:
    """Report whether p4 accepts the depot root as a path to query changelists for."""
    try:
        run(['p4', 'changes', '-m1', '-s', 'submitted', f'{depot_root}/...'],
            cwd=workspace_dir)
        return True
    except CommandError:
        return False


def get_latest_changelist(depot_root: str, workspace_dir: str) -> int:
    """Get the latest submitted changelist affecting the depot root."""
    res = run(['p4', '-ztag', 'changes', '-m1', '-s', 'submitted',
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
    res = run(['p4', '-ztag', 'changes', '-s', 'submitted',
               f'{depot_root}/...@{start},@{end}'], cwd=workspace_dir)
    records = parse_ztag_multi_output(res.stdout)
    changes = [P4Change(change=int(r['change']), user=r.get('user', ''))
               for r in records if 'change' in r]
    return sorted(changes, key=lambda c: c.change)


def get_p4_user(workspace_dir: str) -> str | None:
    """Get the Perforce user name of the current connection."""
    res = run(['p4', '-ztag', 'info'], cwd=workspace_dir)
    return parse_ztag_output(res.stdout).get('userName') or None


def get_existing_p4_users(names: list[str], workspace_dir: str) -> list[str]:
    """Return the names that are Perforce users, spelled as the server spells them."""
    res = run(['p4', '-ztag', 'users', *names], cwd=workspace_dir,
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
    res = run(['p4', '-x', '-', '-ztag', 'fstat', '-Ro', '-Op',
               '-T', 'path,clientFile,action,change'],
              cwd=workspace_dir, input='\n'.join(filenames),
              fail_on_returncode=False)
    # Files that are not opened, or not in the depot yet, are reported per file.
    _warn_p4_errors(res, ('not opened', 'no such file'),
                    'p4 fstat failed to list opened files:')
    return {filename: (record.get('change', ''), record.get('action', ''))
            for filename, record in _fstat_records(res, workspace_dir)}


def _opened_lookup(opened: dict[str, tuple[str, str]]
                   ) -> Callable[[str], tuple[str, str] | None]:
    """Look files up in get_opened_files' result, ignoring case where there is no exact match."""
    # p4 may spell a path in the client's case on case-insensitive file systems.
    folded = {path.casefold(): state for path, state in opened.items()}
    return lambda filename: (opened.get(filename)
                             or folded.get(filename.casefold()))


def _run_p4_batch(p4_args: list[str], filenames: list[str], summary: str,
                  workspace_dir: str, dry_run: bool,
                  fail_on_returncode: bool = True) -> list[str]:
    """List filenames under summary, then run one p4 command over them all; returns p4's output lines."""
    if not filenames:
        return []
    log.info(f'{summary}:')
    for filename in filenames:
        log.info(f'  {filename}')
    result = run(['p4', '-x', '-', *p4_args], cwd=workspace_dir,
                 input='\n'.join(filenames), dry_run=dry_run,
                 fail_on_returncode=fail_on_returncode)
    return result.stdout + result.stderr


def _warn_unopened(to_open: list[tuple[str, str]], changelist: str,
                   p4_output: list[str], workspace_dir: str) -> None:
    """Warn about the files p4 did not open as asked; p4 still exits 0 for most of them."""
    opened = _opened_lookup(
        get_opened_files([f for f, _ in to_open], workspace_dir))
    unopened = [(f, action) for f, action in to_open
                if opened(f) != (changelist, action)]
    if not unopened:
        return
    log.warning(f'p4 did not open {len(unopened)} of {len(to_open)} files:')
    for filename, action in unopened:
        log.info(f'  {filename} (for {action})')
    messages = [line for line in p4_output if 'opened for' not in line]
    if messages:
        log.info('p4 said:')
        for line in messages:
            log.info(f'  {line}')


def include_changes_in_changelist(changes: LocalChanges, changelist: str,
                                  workspace_dir: str, dry_run: bool = False) -> None:
    """Open local git changes for add/edit/delete in a Perforce changelist."""
    wanted = ([(f, 'add') for f in changes.adds]
              + [(f, 'edit') for f in changes.mods]
              + [(f, 'delete') for f in changes.dels])
    for from_filename, to_filename in changes.moves:
        wanted += [(from_filename, 'delete'), (to_filename, 'add')]

    opened = _opened_lookup(
        get_opened_files([f for f, _ in wanted], workspace_dir))
    to_open = []
    to_reopen = []
    to_revert = []
    to_restore = []
    for filename, p4_action in wanted:
        current = opened(filename)
        if current is None:
            to_open.append((filename, p4_action))
            continue
        current_cl, current_action = current
        # add -> edit: new to the depot, so it stays an add.
        if current_action == p4_action or (current_action == 'add'
                                           and p4_action == 'edit'):
            if current_cl != changelist:
                to_reopen.append(filename)
            continue
        # Revert and reopen with the right action, restoring git's content p4 revert overwrote.
        to_revert.append(filename)
        # add -> delete: never in the depot, so just revert.
        if current_action == 'add' and p4_action == 'delete':
            continue
        to_open.append((filename, p4_action))
        if p4_action != 'delete':
            to_restore.append(filename)

    _run_p4_batch(['revert'], to_revert, 'Reverting files opened with another action',
                  workspace_dir, dry_run)
    p4_output = []
    for p4_action in ('add', 'edit', 'delete'):
        p4_output += _run_p4_batch(
            [p4_action, '-c', changelist],
            [f for f, action in to_open if action == p4_action],
            f'Opening for {p4_action}', workspace_dir, dry_run,
            fail_on_returncode=False)
    _run_p4_batch(['reopen', '-c', changelist], to_reopen,
                  'Moving files from other changelists', workspace_dir, dry_run)
    if to_restore:
        restore_files(to_restore, workspace_dir, dry_run)
    if to_open and not dry_run:
        _warn_unopened(to_open, changelist, p4_output, workspace_dir)


def get_opened_files_in_changelist(changelist: str,
                                   workspace_dir: str) -> list[tuple[str, str]]:
    """Return (path, action) for files opened in a changelist, as workspace-relative paths."""
    res = run(['p4', '-ztag', 'fstat', '-Ro', '-e', changelist, '-Op',
               '-T', 'path,clientFile,action', '...'],
              cwd=workspace_dir, fail_on_returncode=False)
    _warn_p4_errors(res, ('not opened',),
                    'p4 fstat failed to list opened files:')
    return [(filename, record.get('action', ''))
            for filename, record in _fstat_records(res, workspace_dir)]


def p4_revert(filenames: list[str], workspace_dir: str,
              dry_run: bool = False) -> None:
    """Revert the given files, writing their depot version to disk."""
    run(['p4', '-x', '-', 'revert'], cwd=workspace_dir,
        input='\n'.join(filenames), dry_run=dry_run)


def p4_revert_unchanged(filenames: list[str], changelist: str,
                        workspace_dir: str, dry_run: bool = False) -> list[str]:
    """Revert files that are unchanged, or missing and opened for add; returns p4's lines."""
    if not filenames:
        return []
    args = ['p4', '-x', '-', 'revert', '-a']
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
    res = run_with_output(
        ['p4', '-ztag', 'fstat', '-Ro', '-Op', '-T',
         'depotFile,path,clientFile,action', f'{depot_root}/...'],
        cwd=workspace_dir)
    return [(filename, _p4_action_to_change(record['action']))
            for filename, record in _fstat_records(res, workspace_dir,
                                                   allow_outside=True)]


# --- shelving ---

def p4_shelve_changelist(changelist: str, workspace_dir: str, dry_run: bool = False) -> None:
    """Shelve a changelist's open files, replacing the shelf and leaving unchanged files out."""
    run(['p4', 'shelve', '-r', '-a', 'leaveunchanged', '-Af',
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
    """Raised to stop a p4 sync that can no longer succeed."""


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
            log.error(line)
            raise P4SyncAbortError(
                'Aborting sync: p4 could not replace a file, most likely '
                'because another process has it open. The workspace is left '
                'partially synced: close that process, then commit or '
                'restore the synced files before syncing again.')

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
    args = ['p4', '-x', '-', '-ztag', 'fstat',
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


def p4_sync_preview(changelist: int, depot_root: str,
                    workspace_dir: str) -> list[P4SyncPreviewFile]:
    """Preview which files would be synced without actually syncing."""
    log.heading(f'Previewing sync to CL {changelist}')
    output_processor = P4SyncOutputProcessor()
    result = run_with_output(
        ['p4', 'sync', '-n', f'{depot_root}/...@{changelist}'],
        cwd=workspace_dir, on_output=output_processor)
    if result.elapsed:
        log.elapsed(result.elapsed)

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
        result = run_with_output(
            ['p4', 'sync', f'{depot_root}/...@{changelist}'],
            cwd=workspace_dir, on_output=output_processor)
        if result.elapsed:
            log.elapsed(result.elapsed)
    except RunError as e:
        refused = get_writable_files(e.stderr)
        if not refused:
            raise
        log.info(output_processor.get_summary())
        raise P4SyncClobberError(e, refused) from e
    log.success(output_processor.get_summary())
