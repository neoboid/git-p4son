"""Git abstraction layer.

All functions that interact directly with the git CLI live here.
"""

import os
import os.path
import re

from .common import CommandError, RunError, run, run_with_output


# --- workspace ---

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


# --- branch ---

def get_current_branch(workspace_dir: str) -> str | None:
    """Return the current git branch name, or None on error/detached HEAD.

    When in detached HEAD during an interactive rebase, returns the
    original branch name from git's rebase state.
    """
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
    """During interactive rebase, read the original branch from git state."""
    try:
        result = run(['git', 'rev-parse', '--git-dir'], cwd=workspace_dir)
        git_dir = result.stdout[0].strip() if result.stdout else None
        if not git_dir:
            return None
        head_name_file = os.path.join(git_dir, 'rebase-merge', 'head-name')
        with open(head_name_file) as f:
            ref = f.read().strip()
        prefix = 'refs/heads/'
        if ref.startswith(prefix):
            return ref[len(prefix):]
        return ref
    except (RunError, FileNotFoundError):
        return None


# --- HEAD ---

def get_head_subject(workspace_dir: str) -> str | None:
    """Return the subject line of the HEAD commit, or None on failure."""
    try:
        result = run(['git', 'log', '-1', '--format=%s', 'HEAD'],
                     cwd=workspace_dir)
        subject = result.stdout[0].strip() if result.stdout else None
        return subject if subject else None
    except (RunError, OSError):
        return None


# --- status ---

def get_dirty_files(workspace_dir: str) -> list[tuple[str, str]]:
    """Return list of (filename, change_type) tuples for dirty files."""
    res = run_with_output(['git', 'status', '--porcelain'], cwd=workspace_dir)
    files = []
    for line in res.stdout:
        status = line[:2].strip()
        filename = line[3:]
        if status == 'A':
            files.append((filename, 'add'))
        elif status == 'D':
            files.append((filename, 'delete'))
        elif status == '??':
            files.append((filename, 'untracked'))
        else:
            files.append((filename, 'modify'))
    return files


# --- staging and committing ---

def add_all_files(workspace_dir: str) -> None:
    """Add all files to git."""
    run_with_output(['git', 'add', '.'], cwd=workspace_dir)


def commit(message: str, workspace_dir: str, allow_empty: bool = False) -> None:
    """Commit changes to git."""
    args = ['commit', '-m', message]
    if allow_empty:
        args.append('--allow-empty')
    run_with_output(['git'] + args, cwd=workspace_dir)


# --- diff ---

class LocalChanges:
    """Container for local git changes."""

    def __init__(self) -> None:
        self.adds: list[str] = []
        self.mods: list[str] = []
        self.dels: list[str] = []
        self.moves: list[tuple[str, str]] = []


def find_common_ancestor(branch1: str, branch2: str, workspace_dir: str) -> str:
    """Find the common ancestor commit between two branches."""
    res = run(['git', 'merge-base', branch1, branch2], cwd=workspace_dir)
    if not res.stdout or len(res.stdout) != 1:
        raise CommandError('git merge-base returned unexpected output')
    return res.stdout[0].strip()


def get_local_changes(base_branch: str, workspace_dir: str) -> LocalChanges:
    """Get local git changes between base_branch and HEAD."""
    ancestor = find_common_ancestor(base_branch, 'HEAD', workspace_dir)

    res = run(['git', 'diff', '--name-status', f'{ancestor}..HEAD'],
              cwd=workspace_dir)

    changes = LocalChanges()
    renamepattern = r"^r(\d+)$"
    for line in res.stdout:
        tokens = line.split('\t')
        status = tokens[0].lower()
        filename = tokens[1]
        if status == 'm':
            changes.mods.append(filename)
        elif status == 'd':
            changes.dels.append(filename)
        elif status == 'a':
            changes.adds.append(filename)
        elif re.search(renamepattern, status):
            from_filename = filename
            to_filename = tokens[2]
            changes.moves.append((from_filename, to_filename))
        else:
            raise CommandError(f'Unknown git status in "{line}"')

    return changes


# --- log ---

def get_commit_lines_since(base_branch: str, workspace_dir: str) -> list[str]:
    """Get git log --oneline lines for commits since base branch."""
    res = run(['git', 'log', '--oneline', '--reverse',
               f'{base_branch}..HEAD'], cwd=workspace_dir)
    return res.stdout


def get_commit_subjects_since(base_branch: str, workspace_dir: str) -> list[str]:
    """Get commit subjects from git log since base branch."""
    lines = get_commit_lines_since(base_branch, workspace_dir)

    subjects = []
    for line in lines:
        if ' ' in line:
            subject = line.split(' ', 1)[1]
            subjects.append(subject)
        else:
            subjects.append(line)

    return subjects


# --- ignore ---

def get_ignored_files(filepaths: list[str], workspace_dir: str) -> set[str]:
    """Return the subset of filepaths that are ignored by git."""
    if not filepaths:
        return set()
    try:
        result = run_with_output(
            ['git', 'check-ignore'] + filepaths, cwd=workspace_dir)
        return set(line.strip() for line in result.stdout if line.strip())
    except RunError:
        # Exit code 1 means no paths matched - that's fine
        return set()


# --- file retrieval ---

def get_file_at_commit(filepath: str, commit: str,
                       workspace_dir: str) -> bytes | None:
    """Retrieve file content at a specific commit. Returns None if the file doesn't exist."""
    # Git uses forward slashes in tree paths, even on Windows
    git_path = filepath.replace('\\', '/')
    result = run(['git', 'show', f'{commit}:{git_path}'],
                 cwd=workspace_dir, text=False, fail_on_returncode=False)
    if result.returncode != 0:
        return None
    return result.stdout


# --- editor ---

def resolve_editor(workspace_dir: str) -> str | None:
    """Resolve the user's editor via git var GIT_EDITOR."""
    try:
        result = run(['git', 'var', 'GIT_EDITOR'], cwd=workspace_dir)
        return result.stdout[0].strip() if result.stdout else None
    except RunError:
        return None
