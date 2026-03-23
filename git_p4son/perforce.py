"""Perforce abstraction layer.

All functions that interact directly with the p4 CLI live here.
"""

import re
from dataclasses import dataclass
from typing import IO

from .common import CommandError, RunError, run, run_with_output
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
    """Parse p4 -ztag output with multiple records into a list of dicts."""
    records = []
    current: dict[str, str] = {}
    for line in lines:
        if line.startswith('... '):
            parts = line[4:].split(' ', 1)
            key = parts[0]
            value = parts[1] if len(parts) > 1 else ''
            current[key] = value
        elif current:
            records.append(current)
            current = {}
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

    if dry_run:
        log.info(f"Would add #review keyword to changelist {changelist}")
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


def _ensure_in_changelist(filename: str, p4_action: str, changelist: str,
                          workspace_dir: str, dry_run: bool) -> None:
    """Ensure a file is opened in the given changelist.

    If the file is not yet opened, run the specified p4 action (add, edit, delete).
    If it's already opened in a different changelist, reopen it.
    If it's already in the correct changelist, do nothing.
    """
    result = get_changelist_for_file(filename, workspace_dir)
    if result is None:
        run(['p4', p4_action, '-c', changelist, filename],
            cwd=workspace_dir, dry_run=dry_run)
    else:
        current_cl, _current_action = result
        if current_cl != changelist:
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


def p4_get_opened_files(depot_root: str, workspace_dir: str) -> list[tuple[str, str]]:
    """Return list of (filename, change_type) tuples for files opened in Perforce."""
    res = run_with_output(
        ['p4', '-ztag', 'opened', f'{depot_root}/...'], cwd=workspace_dir)
    files = []
    for record in parse_ztag_multi_output(res.stdout):
        depot_path = record['depotFile']
        action = record['action']
        if action in ('add', 'move/add'):
            change = 'add'
        elif action in ('delete', 'move/delete'):
            change = 'delete'
        else:
            change = 'modify'
        files.append((depot_path, change))
    return files


# --- shelving ---

def p4_shelve_changelist(changelist: str, workspace_dir: str, dry_run: bool = False) -> None:
    """Shelve a changelist to make it available for review."""
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

    def __init__(self, file_count_to_sync: int) -> None:
        self.synced_file_count: int = 0
        self.file_count_to_sync: int = file_count_to_sync
        self.stats: dict[str, int] = {
            mode: 0 for mode in ['add', 'del', 'upd', 'clb']}

    def __call__(self, line: str, stream: IO[str]) -> None:
        if re.search(r"@\d+ - file\(s\) up-to-date\.", line):
            log.info('all files up to date')
            return

        mode, filename = parse_p4_sync_line(line)
        if not mode or not filename:
            log.verbose(f'Unparsable line: {line}')
            return

        self.stats[mode] += 1
        self.synced_file_count += 1

        if self.file_count_to_sync >= 0:
            log.verbose(
                f'{mode}: {filename}  ({self.synced_file_count}/{self.file_count_to_sync})')
        else:
            log.verbose(f'{mode}: {filename}')

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
    output_processor = P4SyncOutputProcessor(-1)
    result = run_with_output(
        ['p4', 'sync', '-f', f'{filename}@{changelist}'],
        cwd=workspace_dir, on_output=output_processor)
    log.info(output_processor.get_summary())
    if result.elapsed:
        log.elapsed(result.elapsed)


def get_file_count_to_sync(changelist: int, depot_root: str,
                           workspace_dir: str) -> int:
    """Get the number of files that need to be synced."""
    res = run(['p4', 'sync', '-n', f'{depot_root}/...@{changelist}'],
              cwd=workspace_dir)
    return len(res.stdout)
