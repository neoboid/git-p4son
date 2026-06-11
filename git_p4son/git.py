"""Git abstraction layer.

All functions that interact directly with the git CLI live here.
"""

import os
import os.path
import re

from .common import (
    CommandError,
    RunError,
    normalize_workspace_path,
    run,
    run_with_output,
)


# --- workspace ---

def is_workspace_dir(directory: str) -> bool:
    """Check if a directory is a git workspace.

    .git is a directory in a regular repo, but a file pointing at the real
    git dir in linked worktrees and submodules."""
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
        # git prints the dir relative to its cwd (usually just ".git");
        # resolve it against the workspace, not the process cwd.
        head_name_file = os.path.join(workspace_dir, git_dir,
                                      'rebase-merge', 'head-name')
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

def is_file_tracked(filename: str, workspace_dir: str) -> bool:
    """Return whether a file is tracked by git."""
    repo_path = normalize_workspace_path(filename, workspace_dir)
    if repo_path is None:
        return False

    try:
        run(['git', 'ls-files', '--error-unmatch', '--', repo_path],
            cwd=workspace_dir)
        return True
    except RunError:
        return False


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

    # quotepath off: non-ASCII filenames are emitted verbatim instead of
    # C-quoted octal escapes ("b\303\244ck.txt") that would never match a
    # file on disk or in p4.
    res = run(['git', '-c', 'core.quotepath=off', 'diff', '--name-status',
               f'{ancestor}..HEAD'],
              cwd=workspace_dir)

    changes = LocalChanges()
    renamepattern = r"^r(\d+)$"
    copypattern = r"^c(\d+)$"
    for line in res.stdout:
        tokens = line.split('\t')
        status = tokens[0].lower()
        filename = tokens[1]
        if status == 'm':
            changes.mods.append(filename)
        elif status == 't':
            # Typechange (e.g. symlink to regular file): content changed.
            changes.mods.append(filename)
        elif status == 'd':
            changes.dels.append(filename)
        elif status == 'a':
            changes.adds.append(filename)
        elif re.search(renamepattern, status):
            from_filename = filename
            to_filename = tokens[2]
            changes.moves.append((from_filename, to_filename))
        elif re.search(copypattern, status):
            # Copy (with diff.renames=copies): the source is untouched,
            # only the destination is a new file.
            changes.adds.append(tokens[2])
        else:
            raise CommandError(f'Unknown git status in "{line}"')

    return changes


# --- log ---

def get_commit_lines_since(base_branch: str, workspace_dir: str) -> list[str]:
    """Get git log --oneline lines for commits since base branch.

    Merge commits are skipped: as rebase todo picks they fail the rebase
    ("is a merge but no -m option was given"), and the merged branch's own
    commits are already in the range."""
    # Explicit format instead of --oneline: a user's log.decorate=short
    # config would otherwise prepend "(HEAD -> branch)" decorations to
    # every subject in pick lines and changelist descriptions.
    res = run(['git', 'log', '--format=%h %s', '--no-decorate',
               '--reverse', '--no-merges',
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


# --- tracking ---

def get_tracked_files(filepaths: list[str], workspace_dir: str) -> set[str]:
    """Return the subset of filepaths that are tracked by git, batched.

    Tracking status decides whether a file is git's to manage: a tracked
    file matching a .gitignore pattern (common when .gitignore was copied
    from .p4ignore) is still tracked. Returned paths are the input paths."""
    if not filepaths:
        return set()
    by_git_path: dict[str, str] = {}
    for filepath in filepaths:
        git_path = normalize_workspace_path(filepath, workspace_dir)
        if git_path is not None:
            by_git_path[git_path] = filepath
    tracked: set[str] = set()
    chunks = _chunk_paths_by_length(
        list(by_git_path), _PATHSPEC_LENGTH_BUDGET)
    for chunk in chunks:
        # -z output is NUL-separated and verbatim; without it paths with
        # non-ASCII characters are C-quoted and would never match.
        result = run(['git', 'ls-files', '-z', '--'] + chunk,
                     cwd=workspace_dir)
        for line in result.stdout:
            for git_path in line.split('\0'):
                if git_path in by_git_path:
                    tracked.add(by_git_path[git_path])
    return tracked


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


def get_blob_oids(items: list[tuple[str, str]],
                  workspace_dir: str) -> dict[tuple[str, str], str | None]:
    """Blob OIDs for (commit, filepath) pairs, resolved in one git call.

    Returns a mapping of (commit, filepath) to OID, or None when the file
    doesn't exist at that commit. Equal OIDs mean byte-identical content
    (git content-addresses blobs), so this answers "did the content change?"
    without transferring the blobs."""
    if not items:
        return {}
    queries = []
    for commit, filepath in items:
        # Git uses forward slashes in tree paths, even on Windows
        git_path = filepath.replace('\\', '/')
        queries.append(f'{commit}:{git_path}')
    # cat-file --batch-check reads object names from stdin and emits exactly
    # one line per input line, so results map back to items by position.
    result = run(['git', 'cat-file', '--batch-check'], cwd=workspace_dir,
                 input='\n'.join(queries) + '\n')
    oids: dict[tuple[str, str], str | None] = {}
    for item, line in zip(items, result.stdout):
        # Found objects print "<oid> <type> <size>"; anything else
        # (missing, ambiguous) means no blob at that commit.
        parts = line.split()
        if len(parts) == 3 and parts[2].isdigit():
            oids[item] = parts[0]
        else:
            oids[item] = None
    return oids


def get_head_commit(workspace_dir: str) -> str:
    """Return the SHA of HEAD."""
    result = run(['git', 'rev-parse', 'HEAD'], cwd=workspace_dir)
    return result.stdout[0].strip()


# Substring identifying git-p4son sync commit subjects.
SYNC_SUBJECT_MARKER = ': p4 sync //'

# Conservative limit for pathspec arguments per git invocation; Windows
# caps the whole command line at 32767 characters.
_PATHSPEC_LENGTH_BUDGET = 20000


def _chunk_paths_by_length(paths: list[str], budget: int) -> list[list[str]]:
    """Split paths into chunks whose total argument length stays in budget."""
    chunks: list[list[str]] = []
    chunk: list[str] = []
    used = 0
    for path in paths:
        cost = len(path) + 1
        if chunk and used + cost > budget:
            chunks.append(chunk)
            chunk = []
            used = 0
        chunk.append(path)
        used += cost
    if chunk:
        chunks.append(chunk)
    return chunks


def find_base_commits(filepaths: list[str], before_commit: str,
                      workspace_dir: str) -> dict[str, str | None]:
    """Baseline commit for each repo-relative path, batched.

    The baseline is the most recent sync commit reachable from before_commit
    (inclusive) that touched the file, falling back to the most recent commit
    that added it when no sync commit ever touched it (e.g. files brought in
    via an initial bulk import committed with a non-sync subject; if the file
    was deleted and re-added, the most recent add starts the current
    lineage), or None when neither exists.

    All paths are resolved in a single history walk per pathspec chunk
    instead of one git log per file."""
    result: dict[str, str | None] = {}
    if not filepaths:
        return result
    # Git uses forward slashes in tree paths, even on Windows
    by_git_path = {fp.replace('\\', '/'): fp for fp in filepaths}
    chunks = _chunk_paths_by_length(
        list(by_git_path), _PATHSPEC_LENGTH_BUDGET)
    for chunk in chunks:
        chunk_result = _find_base_commits_chunk(
            chunk, before_commit, workspace_dir)
        for git_path, sha in chunk_result.items():
            result[by_git_path[git_path]] = sha
    return result


def _find_base_commits_chunk(git_paths: list[str], before_commit: str,
                             workspace_dir: str) -> dict[str, str | None]:
    """One newest-first history walk resolving baselines for git_paths.

    The first sync commit seen touching a path is its baseline (the most
    recent one). The first add seen is remembered as the fallback for paths
    no sync commit ever touched. core.quotePath is disabled so non-ASCII
    paths in --name-status output match the input verbatim."""
    result: dict[str, str | None] = dict.fromkeys(git_paths)
    res = run(
        ['git', '-c', 'core.quotePath=false', 'log', '--no-renames',
         '--name-status', '--pretty=format:%x01%H%x01%s',
         before_commit, '--'] + git_paths,
        cwd=workspace_dir, fail_on_returncode=False)
    if res.returncode != 0:
        return result

    remaining = set(git_paths)
    fallback_add: dict[str, str] = {}
    current_sha = ''
    current_is_sync = False
    for line in res.stdout:
        if line.startswith('\x01'):
            _, current_sha, subject = line.split('\x01', 2)
            current_is_sync = SYNC_SUBJECT_MARKER in subject
            continue
        status, sep, path = line.partition('\t')
        if not sep or path not in remaining:
            continue
        if current_is_sync:
            result[path] = current_sha
            remaining.discard(path)
        elif status.startswith('A') and path not in fallback_add:
            fallback_add[path] = current_sha
    for path in remaining:
        result[path] = fallback_add.get(path)
    return result


# --- merge ---

def merge_file(current_path: str, base_path: str,
               other_path: str) -> tuple[bool, bytes]:
    """Three-way merge using git merge-file.

    All three inputs are file paths read by git directly. Returns
    (clean, merged_content) where clean is True if no conflicts.
    """
    result = run(
        ['git', 'merge-file', '-p',
         '--marker-size=7',
         '-L', 'Perforce', '-L', 'base', '-L', 'local',
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
