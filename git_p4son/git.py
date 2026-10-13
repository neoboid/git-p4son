"""Git abstraction layer: all direct git CLI calls live here."""

import os
import os.path
import re
from dataclasses import dataclass, field

from .common import (
    CommandError,
    RunError,
    normalize_workspace_path,
    run,
)


# Read as bytes: text mode would turn a carriage return in a path into a newline.
def _nul_fields(stdout: bytes) -> list[str]:
    """Split raw -z output into its fields."""
    return [field for field in stdout.decode('utf-8', 'replace').split('\0')
            if field]


def _git_path(filepath: str) -> str:
    """Git uses forward slashes in tree paths, even on Windows."""
    return filepath.replace('\\', '/')


# --- workspace ---

def is_workspace_dir(directory: str) -> bool:
    """Check if a directory is a git workspace (.git may be a dir or, in worktrees, a file)."""
    return os.path.exists(os.path.join(directory, '.git'))


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


def init_repo(workspace_dir: str) -> None:
    """Create a git repository in workspace_dir."""
    run(['git', 'init'], cwd=workspace_dir)


def has_commits(workspace_dir: str) -> bool:
    """Return whether the repository has any commit (HEAD resolves)."""
    result = run(['git', 'rev-parse', '--verify', '--quiet', 'HEAD'],
                 cwd=workspace_dir, fail_on_returncode=False)
    return result.returncode == 0


# --- branch ---

def get_current_branch(workspace_dir: str) -> str | None:
    """Return the current branch name (the original one during a rebase), or None on detached HEAD."""
    try:
        result = run(['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                     cwd=workspace_dir)
        branch = result.stdout[0].strip() if result.stdout else None
        if branch == 'HEAD':
            return _get_rebase_branch(workspace_dir)
        return branch
    except (RunError, OSError):
        return None


def _get_rebase_branch(workspace_dir: str) -> str | None:
    """During a rebase, read the original branch from git state."""
    # Interactive and merge rebases keep their state in rebase-merge, the apply backend in rebase-apply.
    try:
        result = run(['git', 'rev-parse',
                      '--git-path', 'rebase-merge/head-name',
                      '--git-path', 'rebase-apply/head-name'],
                     cwd=workspace_dir)
    except RunError:
        return None
    for head_name in result.stdout:
        # git prints paths relative to its cwd; resolve them against the workspace.
        try:
            with open(os.path.join(workspace_dir, head_name.strip())) as f:
                return f.read().strip().removeprefix('refs/heads/')
        except FileNotFoundError:
            continue
    return None


# --- HEAD ---

def get_head_subject(workspace_dir: str) -> str | None:
    """Return the subject line of the HEAD commit, or None on failure."""
    try:
        result = run(['git', 'log', '-1', '--format=%s', 'HEAD'],
                     cwd=workspace_dir)
        subject = result.stdout[0].strip() if result.stdout else ''
        return subject or None
    except (RunError, OSError):
        return None


# --- status ---

_PORCELAIN_KINDS = {'A': 'add', 'D': 'delete', '??': 'untracked'}


def get_dirty_files(workspace_dir: str) -> list[tuple[str, str]]:
    """Return list of (filename, change_type) tuples for dirty files."""
    # -z: paths verbatim, not C-quoted, as "XY <path>\0", followed by the old path for renames and copies.
    res = run(['git', 'status', '--porcelain', '-z'], cwd=workspace_dir,
              text=False)
    files = []
    entries = iter(_nul_fields(res.stdout))
    for entry in entries:
        status = entry[:2]
        files.append(
            (entry[3:], _PORCELAIN_KINDS.get(status.strip(), 'modify')))
        if 'R' in status or 'C' in status:
            next(entries)
    return files


# --- staging and committing ---

def add_all_files(workspace_dir: str) -> None:
    """Stage every change in the workspace, untracked files included."""
    run(['git', 'add', '.'], cwd=workspace_dir)


def add_files(filepaths: list[str], workspace_dir: str) -> None:
    """Stage the given files."""
    run(['git', 'add', '--pathspec-from-file=-', '--pathspec-file-nul'],
        cwd=workspace_dir, input='\0'.join(filepaths))


def restore_files(filepaths: list[str], workspace_dir: str,
                  dry_run: bool = False) -> None:
    """Restore the given files in the workspace from the index."""
    run(['git', 'restore', '--pathspec-from-file=-', '--pathspec-file-nul'],
        cwd=workspace_dir, input='\0'.join(filepaths), dry_run=dry_run)


def get_staged_files(workspace_dir: str) -> list[str]:
    """Return the files staged for the next commit, as repo-relative slash paths."""
    # Compares the index with HEAD without scanning the worktree.
    result = run(['git', 'diff', '--cached', '--name-only', '-z'],
                 cwd=workspace_dir, text=False)
    return _nul_fields(result.stdout)


def commit(message: str, workspace_dir: str, allow_empty: bool = False) -> None:
    """Commit the staged changes."""
    command = ['git', 'commit', '-m', message]
    if allow_empty:
        command.append('--allow-empty')
    run(command, cwd=workspace_dir)


# --- diff ---

@dataclass
class LocalChanges:
    """Files changed between the base branch and HEAD, by kind."""
    adds: list[str] = field(default_factory=list)
    mods: list[str] = field(default_factory=list)
    dels: list[str] = field(default_factory=list)
    moves: list[tuple[str, str]] = field(default_factory=list)


def get_local_changes(base_branch: str, workspace_dir: str) -> LocalChanges:
    """Get local git changes between base_branch and HEAD."""
    # ...HEAD diffs from the merge base, so changes on base_branch since the fork are left out.
    # -z: paths verbatim, not C-quoted, as "<status>\0<path>\0", with a second path for renames and copies.
    res = run(['git', 'diff', '--name-status', '-z', f'{base_branch}...HEAD'],
              cwd=workspace_dir, text=False)

    changes = LocalChanges()
    tokens = iter(_nul_fields(res.stdout))
    for token in tokens:
        status = token.lower()
        # T is a typechange (e.g. symlink to regular file): its content changed.
        if status in ('m', 't'):
            changes.mods.append(next(tokens))
        elif status == 'd':
            changes.dels.append(next(tokens))
        elif status == 'a':
            changes.adds.append(next(tokens))
        elif status[:1] == 'r' and status[1:].isdigit():
            changes.moves.append((next(tokens), next(tokens)))
        elif status[:1] == 'c' and status[1:].isdigit():
            # Copy: the source is untouched, only the destination is new.
            next(tokens)
            changes.adds.append(next(tokens))
        else:
            raise CommandError(f'Unknown git status "{token}"')

    return changes


# --- log ---

def get_commit_lines_since(base_branch: str, workspace_dir: str) -> list[str]:
    """Return "<short sha> <subject>" lines for the non-merge commits since base_branch, oldest first."""
    # Not --oneline: log.decorate config would prepend "(HEAD -> branch)" to subjects.
    res = run(['git', 'log', '--format=%h %s', '--no-decorate',
               '--reverse', '--no-merges',
               f'{base_branch}..HEAD'], cwd=workspace_dir)
    return res.stdout


def get_commit_subjects_since(base_branch: str, workspace_dir: str) -> list[str]:
    """Return the subjects of the non-merge commits since base_branch, oldest first."""
    return [line.split(' ', 1)[-1]
            for line in get_commit_lines_since(base_branch, workspace_dir)]


# --- tracking ---

def get_tracked_files(filepaths: list[str], workspace_dir: str) -> set[str]:
    """Return the subset of filepaths that are tracked by git."""
    if not filepaths:
        return set()
    # One listing of the index; pathspecs make git match every entry against every path.
    tracked = set(list_tracked_files(workspace_dir))
    return {filepath for filepath in filepaths
            if normalize_workspace_path(filepath, workspace_dir) in tracked}


def list_tracked_files(workspace_dir: str) -> list[str]:
    """Return every file tracked by git, as repo-relative slash paths."""
    # -z: non-ASCII paths verbatim, not C-quoted.
    result = run(['git', 'ls-files', '-z'], cwd=workspace_dir, text=False)
    return _nul_fields(result.stdout)


# --- file retrieval ---

def _cat_file_input(items: list[tuple[str, str]]) -> str:
    """The "<commit>:<path>" lines git cat-file reads from stdin."""
    return ''.join(f'{commit}:{_git_path(filepath)}\n'
                   for commit, filepath in items)


def get_files_at_commits(items: list[tuple[str, str]],
                         workspace_dir: str) -> dict[tuple[str, str], bytes | None]:
    """Map (commit, filepath) pairs to file content, or None where the file doesn't exist at that commit."""
    if not items:
        return {}
    # --batch answers each input line in order, so results map back by position.
    result = run(['git', 'cat-file', '--batch'], cwd=workspace_dir,
                 input=_cat_file_input(items), text=False)
    out = result.stdout
    contents: dict[tuple[str, str], bytes | None] = {}
    pos = 0
    for item in items:
        end = out.index(b'\n', pos)
        header = out[pos:end].split()
        pos = end + 1
        # Found objects print "<oid> <type> <size>", then the content and a newline; missing ones only a line.
        contents[item] = None
        if len(header) == 3 and header[2].isdigit():
            size = int(header[2])
            if header[1] == b'blob':
                contents[item] = out[pos:pos + size]
            pos += size + 1
    return contents


def get_blob_oids(items: list[tuple[str, str]],
                  workspace_dir: str) -> dict[tuple[str, str], str | None]:
    """Map (commit, filepath) pairs to blob OIDs, or None where the file doesn't exist at that commit."""
    if not items:
        return {}
    # --batch-check emits one line per input line, so results map back by position.
    result = run(['git', 'cat-file', '--batch-check'], cwd=workspace_dir,
                 input=_cat_file_input(items))
    oids: dict[tuple[str, str], str | None] = {}
    for item, line in zip(items, result.stdout):
        # Found objects print "<oid> <type> <size>"; anything else means no blob.
        parts = line.split()
        oids[item] = parts[0] if len(
            parts) == 3 and parts[2].isdigit() else None
    return oids


def get_head_commit(workspace_dir: str) -> str:
    """Return the SHA of HEAD."""
    result = run(['git', 'rev-parse', 'HEAD'], cwd=workspace_dir)
    return result.stdout[0].strip()


# --- sync commits ---

_SYNC_SUBJECT_PREFIX = 'git-p4son: p4 sync '
_SYNC_SUBJECT_RE = re.compile(
    '^' + re.escape(_SYNC_SUBJECT_PREFIX) + r'//.+@(\d+)$')


def format_sync_subject(depot_root: str, changelist: int) -> str:
    """Subject of the commit sync makes for a changelist."""
    return f'{_SYNC_SUBJECT_PREFIX}{depot_root}/...@{changelist}'


def parse_sync_subject(subject: str) -> int | None:
    """Return the changelist of a sync commit subject, or None for any other subject."""
    match = _SYNC_SUBJECT_RE.match(subject)
    return int(match.group(1)) if match else None


@dataclass
class LastSync:
    """Info about the most recent p4son sync commit."""
    changelist: int
    commit: str


def git_last_sync(workspace_dir: str) -> LastSync | None:
    """Get the changelist number and commit SHA of the most recent sync commit."""
    res = run(
        ['git', 'log', '-1', '--pretty=%H %s',
         f'--grep=^{_SYNC_SUBJECT_PREFIX}'],
        cwd=workspace_dir)
    if not res.stdout:
        return None

    commit_hash, _, subject = res.stdout[0].partition(' ')
    changelist = parse_sync_subject(subject)
    if changelist is None:
        return None
    return LastSync(changelist=changelist, commit=commit_hash)


def find_base_commits(filepaths: list[str], before_commit: str,
                      workspace_dir: str) -> dict[str, str | None]:
    """Map each path to its most recent sync commit before before_commit, else its latest add, else None."""
    result: dict[str, str | None] = dict.fromkeys(filepaths)
    if not filepaths:
        return result
    by_git_path = {_git_path(fp): fp for fp in filepaths}
    # Paths go on stdin after `--`, so any number of them takes a single history walk.
    res = run(['git', 'log', '-z', '--no-renames', '--name-status',
               '--pretty=format:%x01%H%x01%s', '--stdin', before_commit],
              cwd=workspace_dir, input='--\n' + '\n'.join(by_git_path) + '\n',
              text=False, fail_on_returncode=False)
    if res.returncode != 0:
        return result

    remaining = set(by_git_path)
    fallback_add: dict[str, str] = {}
    current_sha = ''
    current_is_sync = False
    tokens = iter(_nul_fields(res.stdout))
    for token in tokens:
        # -z: "<status>\0<path>\0" pairs, each commit's header joined by a newline to its first status.
        status = token
        if token.startswith('\x01'):
            header, _, status = token.partition('\n')
            _, current_sha, subject = header.split('\x01', 2)
            current_is_sync = parse_sync_subject(subject) is not None
            if not status:
                continue
        path = next(tokens, '')
        if path not in remaining:
            continue
        if current_is_sync:
            result[by_git_path[path]] = current_sha
            remaining.discard(path)
        elif status.startswith('A') and path not in fallback_add:
            fallback_add[path] = current_sha
    for path in remaining:
        result[by_git_path[path]] = fallback_add.get(path)
    return result


# --- merge ---

def merge_file(current_path: str, base_path: str, other_path: str,
               labels: tuple[str, str, str]) -> tuple[bool, bytes]:
    """Three-way merge of file paths using git merge-file; returns (clean, merged_content)."""
    current_label, base_label, other_label = labels
    result = run(
        ['git', 'merge-file', '-p',
         '--marker-size=7',
         '-L', current_label, '-L', base_label, '-L', other_label,
         current_path, base_path, other_path],
        text=False, fail_on_returncode=False)
    # git merge-file -p: exit 0 = clean, >0 = conflicts (count), <0 = error
    return (result.returncode == 0, result.stdout)


# --- editor ---

def resolve_editor(workspace_dir: str) -> str | None:
    """Resolve the user's editor via git var GIT_EDITOR."""
    try:
        result = run(['git', 'var', 'GIT_EDITOR'], cwd=workspace_dir)
        return result.stdout[0].strip() if result.stdout else None
    except RunError:
        return None
