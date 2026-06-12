"""Perforce abstraction layer.

All functions that interact directly with the p4 CLI live here.
"""

import re
import sys
from dataclasses import dataclass
from typing import IO

from .common import (
    CommandError,
    RunError,
    normalize_workspace_path,
    run,
    run_with_output,
)
from .git import LocalChanges
from .log import log


# --- ztag output parsing ---

def parse_ztag_output(lines: list[str]) -> dict[str, str]:
    """Parse p4 -ztag output into a dict."""
    fields = {}
    for line in lines:
        if line.startswith('... '):
            parts = line[4:].split(' ', 1)
            key = parts[0]
            value = parts[1] if len(parts) > 1 else ''
            fields[key] = value
    return fields


def parse_ztag_multi_output(lines: list[str]) -> list[dict[str, str]]:
    """Parse p4 -ztag output with multiple records into a list of dicts.

    Records are separated by blank lines. A non-blank line without the
    '... ' prefix is a continuation of the previous field's value (e.g.
    a multiline desc), not a record boundary."""
    records = []
    current: dict[str, str] = {}
    last_key: str | None = None
    for line in lines:
        if line.startswith('... '):
            parts = line[4:].split(' ', 1)
            key = parts[0]
            value = parts[1] if len(parts) > 1 else ''
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

@dataclass
class P4ClientSpec:
    """Parsed Perforce client specification."""

    name: str
    root: str
    options: list[str]
    stream: str | None
    line_end: str

    @property
    def clobber(self) -> bool:
        return 'clobber' in self.options


def get_client_spec(cwd: str) -> P4ClientSpec | None:
    """Get the client spec, or None if not in a valid workspace.

    Uses the presence of 'Update' in the ztag output to distinguish a real
    workspace from the default spec p4 returns when CWD is outside any workspace.
    """
    result = run(['p4', '-ztag', 'client', '-o'], cwd=cwd)
    fields = parse_ztag_output(result.stdout)
    if 'Update' not in fields:
        return None
    return P4ClientSpec(
        name=fields['Client'],
        root=fields['Root'],
        options=fields['Options'].split(),
        stream=fields.get('Stream'),
        line_end=fields.get('LineEnd', 'local'),
    )


# --- changelist spec parsing ---

def find_line_starting_with(lines: list[str], prefix: str) -> int:
    """Find the index of the first line starting with prefix, or len(lines) if not found."""
    for i, line in enumerate(lines):
        if line.startswith(prefix):
            return i
    return len(lines)


def find_end_of_indented_section(lines: list[str], start: int) -> int:
    """Find the end of a tab-indented section starting at the given index."""
    i = start
    while i < len(lines) and lines[i].startswith('\t'):
        i += 1
    return i


def extract_description_lines(spec_text: str) -> list[str]:
    """
    Extract the Description field from a p4 changelist spec.

    The spec has tab-indented continuation lines under the Description: header.

    Returns:
        List of description lines with tabs stripped.
    """
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


def add_review_keyword_to_changelist(changelist: str, workspace_dir: str,
                                     dry_run: bool = False) -> None:
    """Add the #review keyword to a changelist description."""
    if dry_run:
        # Checked first: a dry run must not query the server, and the
        # changelist may be a placeholder from a dry-run create.
        log.info(f"Would add #review keyword to changelist {changelist}")
        return

    # Get current changelist description
    res = run(['p4', 'change', '-o', changelist], cwd=workspace_dir)

    lines = res.stdout
    desc_start = find_line_starting_with(lines, 'Description:')
    if desc_start >= len(lines):
        raise CommandError('No Description: field found in changelist spec')

    desc_end = find_end_of_indented_section(lines, desc_start + 1)

    # Check if #review is already in the description
    if any('#review' in line for line in lines[desc_start:desc_end]):
        log.info(f'Changelist {changelist} already has #review keyword')
        return

    # Insert #review at the end of the description, preceded by a blank line
    lines[desc_end:desc_end] = ['\t', '\t#review']

    run(['p4', 'change', '-i'], cwd=workspace_dir,
        input='\n'.join(lines))


def get_latest_changelist(depot_root: str, workspace_dir: str) -> int:
    """Get the latest submitted changelist affecting the depot root."""
    res = run(['p4', '-ztag', 'changes', '-m1', '-s', 'submitted',
              f'{depot_root}/...#head'], cwd=workspace_dir)
    fields = parse_ztag_output(res.stdout)
    if 'change' not in fields:
        raise CommandError('No changelists found affecting workspace')
    return int(fields['change'])


# --- file operations ---

def get_changelist_for_file(filename: str, workspace_dir: str) -> tuple[str, str] | None:
    """Return (changelist, action) for an opened file, or None if not opened."""
    res = run(['p4', '-ztag', 'opened', filename], cwd=workspace_dir)
    fields = parse_ztag_output(res.stdout)
    change = fields.get('change')
    if change is None:
        return None
    return (change, fields.get('action', ''))


def _open_in_changelist(filename: str, p4_action: str, changelist: str,
                        workspace_dir: str, dry_run: bool) -> None:
    """Run a p4 open action (add/edit/delete) and warn if it did not open.

    Per-file problems must not abort the whole command: p4 exits 0 for
    some ("can't add existing file", "file(s) not in client view") and
    non-zero for others ("ignored file can't be added" for files matching
    .p4ignore), only printing the reason either way. A successful open is
    confirmed by its "opened for" output line; anything else is surfaced
    as a warning with p4's message so the user can act on it."""
    result = run(['p4', p4_action, '-c', changelist, filename],
                 cwd=workspace_dir, dry_run=dry_run,
                 fail_on_returncode=False)
    if dry_run:
        return
    output = result.stdout + result.stderr
    if not any('opened for' in line for line in output):
        log.warning(f'p4 {p4_action} did not open {filename}:')
        for line in output:
            log.info(f'  {line}')


def _ensure_in_changelist(filename: str, p4_action: str, changelist: str,
                          workspace_dir: str, dry_run: bool) -> None:
    """Ensure a file is opened with the correct action in the given changelist.

    If the file is not yet opened, run the specified p4 action (add, edit, delete).
    If it's already opened with a different action, revert and reopen.
    If it's already opened with the correct action in a different changelist, reopen it.
    If it's already in the correct changelist with the correct action, do nothing.
    """
    result = get_changelist_for_file(filename, workspace_dir)
    if result is None:
        _open_in_changelist(filename, p4_action, changelist,
                            workspace_dir, dry_run)
        return

    current_cl, current_action = result
    if current_action != p4_action:
        # Action mismatch - revert first, then reopen with correct action.
        # p4 revert overwrites the file on disk with the depot version,
        # so we need git restore afterwards to get the git content back.

        # add -> edit: the file is new to the depot, so it must stay as add.
        # This happens when a file is added in one commit and modified in the next.
        if current_action == 'add' and p4_action == 'edit':
            if current_cl != changelist:
                run(['p4', 'reopen', '-c', changelist, filename],
                    cwd=workspace_dir, dry_run=dry_run)
            return

        run(['p4', 'revert', filename], cwd=workspace_dir, dry_run=dry_run)
        # For add -> delete: the file never existed in the depot, so just revert.
        if current_action == 'add' and p4_action == 'delete':
            return
        _open_in_changelist(filename, p4_action, changelist,
                            workspace_dir, dry_run)
        if p4_action != 'delete':
            run(['git', 'restore', filename],
                cwd=workspace_dir, dry_run=dry_run)
    elif current_cl != changelist:
        run(['p4', 'reopen', '-c', changelist, filename],
            cwd=workspace_dir, dry_run=dry_run)


def include_changes_in_changelist(changes: LocalChanges, changelist: str,
                                  workspace_dir: str, dry_run: bool = False) -> None:
    """Open local git changes for add/edit/delete in a Perforce changelist."""
    for filename in changes.adds:
        _ensure_in_changelist(filename, 'add', changelist,
                              workspace_dir, dry_run)

    for filename in changes.mods:
        _ensure_in_changelist(filename, 'edit', changelist,
                              workspace_dir, dry_run)

    for filename in changes.dels:
        _ensure_in_changelist(filename, 'delete',
                              changelist, workspace_dir, dry_run)

    for from_filename, to_filename in changes.moves:
        _ensure_in_changelist(from_filename, 'delete',
                              changelist, workspace_dir, dry_run)
        _ensure_in_changelist(to_filename, 'add',
                              changelist, workspace_dir, dry_run)


def _p4_action_to_change(action: str) -> str:
    """Convert a p4 action to a git-p4son change label."""
    if action in ('add', 'move/add'):
        return 'add'
    if action in ('delete', 'move/delete'):
        return 'delete'
    return 'modify'


def p4_get_opened_files(depot_root: str,
                        workspace_dir: str) -> list[tuple[str, str]]:
    """Return client paths and change types for files opened in Perforce."""
    res = run_with_output(
        ['p4', '-ztag', 'fstat', '-Ro', '-Op', '-T',
         'depotFile,path,clientFile,action', f'{depot_root}/...'],
        cwd=workspace_dir)
    files = []
    for record in parse_ztag_multi_output(res.stdout):
        client_path = record.get('path') or record.get('clientFile')
        if not client_path:
            client_path = record['depotFile']
        filename = normalize_workspace_path(
            client_path, workspace_dir, allow_outside=True)
        change = _p4_action_to_change(record['action'])
        files.append((filename, change))
    return files


# --- shelving ---

def p4_shelve_changelist(changelist: str, workspace_dir: str, dry_run: bool = False) -> None:
    """Shelve a changelist to make it available for review.

    Existing shelved files are deleted first so the shelf mirrors the
    currently open files: shelve -f only overwrites files still open and
    would leave stale entries for files no longer open (e.g. a file added
    in one commit and deleted in a later one). The delete exits non-zero
    when nothing is shelved yet, which is not an error here."""
    run(['p4', 'shelve', '-d', '-c', changelist],
        cwd=workspace_dir, dry_run=dry_run, fail_on_returncode=False)
    run(['p4', 'shelve', '-f', '-Af', '-c', changelist],
        cwd=workspace_dir, dry_run=dry_run)


# --- sync ---

def get_writable_files(stderr_lines: list[str]) -> list[str]:
    """Extract writable files from p4 sync stderr output."""
    prefix = "Can't clobber writable file "
    return [line[len(prefix):].rstrip()
            for line in stderr_lines if line.startswith(prefix)]


def parse_p4_sync_line(line: str) -> tuple[str | None, str | None]:
    """Parse a line from p4 sync output."""
    patterns = [
        ('add', ' - added as '),
        ('del', ' - deleted as '),
        ('upd', ' - updating '),
        ('clb', "Can't clobber writable file ")
    ]
    for mode, pattern in patterns:
        tokens = line.split(pattern)
        if len(tokens) == 2:
            return (mode, tokens[1])

    return (None, None)


class P4SyncOutputProcessor:
    """Process p4 sync output in real-time."""

    def __init__(self) -> None:
        self.synced_file_count: int = 0
        self.stats: dict[str, int] = {
            mode: 0 for mode in ['add', 'del', 'upd', 'clb']}

    def __call__(self, line: str, stream: IO[str]) -> None:
        if re.search(r"@\d+ - file\(s\) up-to-date\.", line):
            log.info('all files up to date')
            return

        mode, filename = parse_p4_sync_line(line)
        if not mode or not filename:
            log.warning(f'Unparsable line: {line}')
            return

        self.stats[mode] += 1
        self.synced_file_count += 1

    def get_summary(self) -> str:
        """Get a one-line sync summary."""
        synced_count = self.stats['add'] + \
            self.stats['upd'] - self.stats['clb']
        parts = []
        if self.stats['add']:
            parts.append(f"add: {self.stats['add']}")
        if self.stats['upd']:
            parts.append(f"upd: {self.stats['upd']}")
        if self.stats['del']:
            parts.append(f"del: {self.stats['del']}")
        if self.stats['clb']:
            parts.append(f"clb: {self.stats['clb']}")
        detail = ', '.join(parts)
        if detail:
            return f'synced {synced_count} files ({detail})'
        return f'synced {synced_count} files'


def p4_force_sync_file(changelist: int, filename: str, workspace_dir: str) -> None:
    """Force sync a single file."""
    output_processor = P4SyncOutputProcessor()
    result = run_with_output(
        ['p4', 'sync', '-f', f'{filename}@{changelist}'],
        cwd=workspace_dir, on_output=output_processor)
    log.info(output_processor.get_summary())
    if result.elapsed:
        log.elapsed(result.elapsed)
