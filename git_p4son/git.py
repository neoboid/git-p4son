"""Git abstraction layer: all direct git CLI calls live here."""

import contextlib
import os
import os.path
import re
import tempfile
from collections.abc import Iterator
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
    # The full ref, not rev-parse --abbrev-ref, which says heads/main when a tag is called main too.
    try:
        result = run(['git', 'symbolic-ref', '-q', 'HEAD'],
                     cwd=workspace_dir, fail_on_returncode=False)
    except OSError:
        return None
    if result.returncode == 0 and result.stdout:
        return result.stdout[0].strip().removeprefix('refs/heads/')
    return _get_rebase_branch(workspace_dir)


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

def get_head_subject(workspace_dir: str, revision: str = 'HEAD') -> str | None:
    """Return the subject line of revision's commit, HEAD by default, or None on failure."""
    try:
        result = run(['git', 'log', '-1', '--format=%s', revision],
                     cwd=workspace_dir)
        subject = result.stdout[0].strip() if result.stdout else ''
        return subject or None
    except (RunError, OSError):
        return None


# --- status ---

_PORCELAIN_KINDS = {'A': 'add', 'D': 'delete', '??': 'untracked'}


@contextlib.contextmanager
def _index_from(revision: str, workspace_dir: str) -> Iterator[dict[str, str]]:
    """Yield the environment for git commands to use a fresh index read from revision, leaving the real one alone."""
    with tempfile.TemporaryDirectory(prefix='git-p4son-index-') as directory:
        env = {'GIT_INDEX_FILE': os.path.join(directory, 'index')}
        run(['git', 'read-tree', revision], cwd=workspace_dir, env=env)
        yield env


def get_dirty_files(workspace_dir: str) -> list[tuple[str, str]]:
    """Return list of (filename, change_type) tuples for dirty files."""
    # -z: paths verbatim, not C-quoted, as "XY <path>\0", followed by the old path for renames and copies.
    res = run(['git', 'status', '--porcelain', '-z'],
              cwd=workspace_dir, text=False)
    files = []
    entries = iter(_nul_fields(res.stdout))
    for entry in entries:
        status = entry[:2]
        files.append(
            (entry[3:], _PORCELAIN_KINDS.get(status.strip(), 'modify')))
        if 'R' in status or 'C' in status:
            next(entries)
    return files


def get_changed_files(revision: str, workspace_dir: str, eol_conversion: str | None = None) -> list[str]:
    """The tracked files whose working contents differ from revision, read with jj's eol_conversion if given."""
    # git diff re-reads files whose stat data the index no longer matches, as jj leaves it, so contents decide.
    res = run([*_git_for(eol_conversion), 'diff', '--name-only', '-z', revision, '--'],
              cwd=workspace_dir, text=False)
    return _nul_fields(res.stdout)


def get_untracked_files(revision: str, workspace_dir: str) -> list[str]:
    """List the files git does not ignore that revision does not hold, leaving the real index alone."""
    with _index_from(revision, workspace_dir) as env:
        result = run(['git', 'ls-files', '--others', '--exclude-standard', '-z'],
                     cwd=workspace_dir, env=env, text=False)
    return _nul_fields(result.stdout)


# --- staging and committing ---

# jj's working-copy.eol-conversion values, as git's core.autocrlf.
_AUTOCRLF = {'none': 'false', 'input': 'input', 'input-output': 'true'}
# And back: the eol-conversion that matches a core.autocrlf.
EOL_FOR_AUTOCRLF = {value: eol for eol, value in _AUTOCRLF.items()}
# The ways git spells a boolean, which core.autocrlf takes besides input.
_GIT_BOOLEANS = {'true': 'true', 'yes': 'true', 'on': 'true', '1': 'true',
                 'false': 'false', 'no': 'false', 'off': 'false', '0': 'false'}


def get_autocrlf(workspace_dir: str) -> str:
    """git's core.autocrlf for workspace_dir as true, false or input; unset means false."""
    # Each entry is the key, then a newline and the value if it has one, then NUL; git uses the last entry.
    result = run(['git', 'config', '-z', '--get-regexp', r'^core\.autocrlf$'],
                 cwd=workspace_dir, text=False, fail_on_returncode=False)
    entries = [entry for entry in result.stdout.split(b'\0') if entry]
    if not entries:
        return 'false'
    _key, newline, value = entries[-1].decode(
        'utf-8', 'replace').partition('\n')
    # A bare key, autocrlf alone on its line, is true; an empty value, autocrlf =, is false.
    if not newline:
        return 'true'
    value = value.strip().lower()
    return 'input' if value == 'input' else _GIT_BOOLEANS.get(value, 'false')


def _git_for(eol_conversion: str | None) -> list[str]:
    """The git command, with core.autocrlf matching jj's working-copy.eol-conversion if given."""
    if eol_conversion is None:
        return ['git']
    return ['git', '-c', f'core.autocrlf={_AUTOCRLF[eol_conversion]}']


def checkout_detached(commit: str, workspace_dir: str, eol_conversion: str | None = None) -> None:
    """Put commit's files in the work tree, overwriting changes, and detach HEAD there, writing jj's line endings."""
    run([*_git_for(eol_conversion), 'checkout', '--quiet',
        '--force', '--detach', commit], cwd=workspace_dir)


def reset_files_commands(commit: str, index_commit: str, eol_conversion: str | None = None) -> list[list[str]]:
    """The git commands that put commit's files on disk, with jj's line endings, and index_commit in the index."""
    return [[*_git_for(eol_conversion), 'read-tree', '-u', '--reset', commit],
            ['git', 'read-tree', index_commit]]


def add_all_files(workspace_dir: str, eol_conversion: str | None = None) -> None:
    """Stage every change in the workspace, untracked files included, converting line endings as jj would if given."""
    run([*_git_for(eol_conversion), 'add', '.'], cwd=workspace_dir)


def add_files(filepaths: list[str], workspace_dir: str) -> None:
    """Stage the given files."""
    run(['git', 'add', '--pathspec-from-file=-', '--pathspec-file-nul'],
        cwd=workspace_dir, input='\0'.join(filepaths))


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


def commit_all(message: str, workspace_dir: str, eol_conversion: str | None = None) -> int:
    """Stage everything as add_all_files does and commit it, even if nothing changed; returns how many files."""
    add_all_files(workspace_dir, eol_conversion)
    staged = get_staged_files(workspace_dir)
    commit(message, workspace_dir, allow_empty=True)
    return len(staged)


# --- diff ---

@dataclass
class LocalChanges:
    """Files changed between the base branch and HEAD, by kind."""
    adds: list[str] = field(default_factory=list)
    mods: list[str] = field(default_factory=list)
    dels: list[str] = field(default_factory=list)
    moves: list[tuple[str, str]] = field(default_factory=list)


def get_local_changes(base_branch: str, workspace_dir: str,
                      revision: str = 'HEAD') -> LocalChanges:
    """Get local git changes between base_branch and revision."""
    # ... diffs from the merge base, so changes on base_branch since the fork are left out.
    # -z: paths verbatim, not C-quoted, as "<status>\0<path>\0", with a second path for renames and copies.
    res = run(['git', 'diff', '--name-status', '-z', f'{base_branch}...{revision}'],
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

def get_commits_since(base: str, workspace_dir: str,
                      revision: str = 'HEAD') -> list[tuple[str, str]]:
    """Return (sha, subject) for the non-merge commits since base, oldest first."""
    # Not --oneline: log.decorate config would prepend "(HEAD -> branch)" to subjects.
    res = run(['git', 'log', '--format=%H %s', '--no-decorate',
               '--reverse', '--no-merges', f'{base}..{revision}'],
              cwd=workspace_dir)
    return [(sha, subject) for sha, _, subject
            in (line.partition(' ') for line in res.stdout)]


def get_commit_subjects_since(base_branch: str, workspace_dir: str,
                              revision: str = 'HEAD') -> list[str]:
    """Return the subjects of the non-merge commits since base_branch, oldest first, or a short SHA for none."""
    return [commit_subject(sha, subject)
            for sha, subject in get_commits_since(base_branch, workspace_dir, revision)]


def commit_subject(sha: str, subject: str) -> str:
    """The subject a commit list shows for a commit, its short SHA when it has none."""
    return subject or sha[:7]


# --- tracking ---

def get_tracked_files(filepaths: list[str], workspace_dir: str,
                      revision: str | None = None) -> set[str]:
    """Return the subset of filepaths that git tracks, in the index or else in revision."""
    if not filepaths:
        return set()
    # One listing; pathspecs make git match every entry against every path.
    tracked = set(list_tracked_files(workspace_dir, revision))
    return {filepath for filepath in filepaths
            if normalize_workspace_path(filepath, workspace_dir) in tracked}


def list_tracked_files(workspace_dir: str,
                       revision: str | None = None) -> list[str]:
    """Return every file git tracks, in the index or else in revision, as repo-relative slash paths."""
    # -z: non-ASCII paths verbatim, not C-quoted.
    command = (['git', 'ls-files', '-z'] if revision is None
               else ['git', 'ls-tree', '-r', '--name-only', '-z', revision])
    result = run(command, cwd=workspace_dir, text=False)
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


def resolve_commit(revision: str, workspace_dir: str) -> str:
    """Return the SHA of the commit revision names; raise CommandError if it names none."""
    result = run(['git', 'rev-parse', '--verify', '--quiet', f'{revision}^{{commit}}'],
                 cwd=workspace_dir, fail_on_returncode=False)
    if result.returncode != 0 or not result.stdout:
        raise CommandError(f'"{revision}" is not a commit')
    return result.stdout[0].strip()


def switch_detached(commit: str, workspace_dir: str) -> None:
    """Check out commit with a detached HEAD."""
    run(['git', 'switch', '--quiet', '--detach', commit], cwd=workspace_dir)


def switch_branch(branch: str, workspace_dir: str) -> None:
    """Check out branch."""
    run(['git', 'switch', '--quiet', branch], cwd=workspace_dir)


def branch_exists(branch: str, workspace_dir: str) -> bool:
    """Whether a local branch of that name exists."""
    result = run(['git', 'rev-parse', '--verify', '--quiet', f'refs/heads/{branch}'],
                 cwd=workspace_dir, fail_on_returncode=False)
    return result.returncode == 0


def branches_at(commit: str, workspace_dir: str) -> list[str]:
    """The local branches pointing at commit, by name."""
    # Full names, as a tag of the same name makes refname:short say heads/<name>.
    result = run(['git', 'for-each-ref', f'--points-at={commit}', '--format=%(refname)', 'refs/heads/'],
                 cwd=workspace_dir)
    return [ref.removeprefix('refs/heads/') for ref in result.stdout]


def author_dates(commits: list[str], workspace_dir: str) -> list[str]:
    """The commits' author dates as git stores them, seconds since the epoch and a time zone, in their order."""
    if not commits:
        return []
    return run(['git', 'log', '--no-walk=unsorted', '--format=%ad', '--date=raw', *commits],
               cwd=workspace_dir).stdout


def is_ancestor(commit: str, other: str, workspace_dir: str) -> bool:
    """Whether commit is other or in its history."""
    return run(['git', 'merge-base', '--is-ancestor', commit, other], cwd=workspace_dir,
               fail_on_returncode=False).returncode == 0


def merge_base(first: str, second: str, workspace_dir: str) -> str:
    """The best common ancestor of two commits."""
    return run(['git', 'merge-base', first, second], cwd=workspace_dir).stdout[0]


def _ignored_untracked(workspace_dir: str, *, with_case: bool, env: dict[str, str] | None) -> set[str]:
    """The untracked paths git ignores, a wholly ignored folder as one path ending in /."""
    result = run(['git', *_case(with_case), 'ls-files', '--others', '--ignored', '--exclude-standard', '--directory',
                  '-z'], cwd=workspace_dir, env=env, text=False)
    return set(_nul_fields(result.stdout))


@contextlib.contextmanager
def _index_without(revision: str, files: list[str], workspace_dir: str) -> Iterator[dict[str, str]]:
    """Yield the environment for git commands to use an index of revision's files except files."""
    with _index_from(revision, workspace_dir) as env:
        run(['git', 'update-index', '--force-remove', '-z', '--stdin'], cwd=workspace_dir, env=env,
            input='\0'.join(files))
        yield env


def case_only_ignored(workspace_dir: str, untracked_at: tuple[str, list[str]] | None = None) -> list[str]:
    """The untracked paths git ignores only by matching without case, the deepest folder of each, ending in /.

    With untracked_at, a revision and files, the index holds the revision's files except those."""
    result = run(['git', 'config', '--type=bool', '--get', 'core.ignorecase'],
                 cwd=workspace_dir, fail_on_returncode=False)
    if result.stdout != ['true']:
        return []
    index = contextlib.nullcontext(
        None) if untracked_at is None else _index_without(*untracked_at, workspace_dir)
    with index as env:
        # git's walk, not check-ignore, which can call a folder ignored that the walk, and jj, look into.
        without_case = _ignored_untracked(
            workspace_dir, with_case=False, env=env)
        with_case = _ignored_untracked(workspace_dir, with_case=True, env=env)
    paths = sorted(without_case - with_case)
    return [path for position, path in enumerate(paths)
            if not (path.endswith('/') and position + 1 < len(paths) and paths[position + 1].startswith(path))]


def added_files(commit: str, workspace_dir: str) -> list[str]:
    """The files commit adds to its first parent, or every file of a commit without one."""
    result = run(['git', 'diff-tree', '-r', '--root', '--no-commit-id', '--no-renames', '--name-only',
                  '--diff-filter=A', '-z', commit], cwd=workspace_dir, text=False)
    return _nul_fields(result.stdout)


def _case(with_case: bool) -> list[str]:
    """The git options that match ignore patterns with case if with_case, else by core.ignorecase."""
    return ['-c', 'core.ignorecase=false'] if with_case else []


def ignored_files(paths: list[str], workspace_dir: str, with_case: bool = False) -> list[str]:
    """The paths git's ignore rules match, tracked or not; a path ending in / as a folder."""
    if not paths:
        return []
    # Exit 1 when none matches.
    result = run(['git', *_case(with_case), 'check-ignore', '--no-index', '--stdin', '-z'], cwd=workspace_dir,
                 input='\0'.join(paths), text=False, fail_on_returncode=False)
    if result.returncode > 1:
        raise RunError('git check-ignore', result.returncode,
                       result.stderr.decode('utf-8', 'replace').splitlines())
    return _nul_fields(result.stdout)


def case_only(paths: list[str], workspace_dir: str) -> list[str]:
    """The paths git ignores by core.ignorecase but not when it matches with case."""
    ignored = ignored_files(paths, workspace_dir)
    with_case = set(ignored_files(ignored, workspace_dir, with_case=True))
    return [path for path in ignored if path not in with_case]


def changed_paths(old: str, new: str, workspace_dir: str) -> set[str]:
    """The paths whose contents differ between two commits."""
    # Both paths of a rename, as Perforce has the file at the old one until the change is submitted.
    result = run(['git', 'diff', '--name-only', '--no-renames', '-z', old, new, '--'],
                 cwd=workspace_dir, text=False)
    return set(_nul_fields(result.stdout))


def insert_empty_commit(after: str, message: str, workspace_dir: str) -> tuple[str, str, list[str]]:
    """Commit an empty commit right after after, and recreate HEAD's commits after it on top, moving HEAD.

    The recreated commits are copies with their parent swapped, so the working files and the index stay as they are.
    Returns the new commit, HEAD's old commit, and the other local branches still built on the commits it recreated.
    Raises
    CommandError if a merge commit is among them."""
    head = get_head_commit(workspace_dir)
    commits = run(['git', 'rev-list', '--reverse',
                  f'{after}..{head}'], cwd=workspace_dir).stdout
    if run(['git', 'rev-list', '--merges', f'{after}..{head}'], cwd=workspace_dir).stdout:
        raise CommandError(
            f'A merge commit follows {after[:7]}, so no commit can be inserted after it.')
    inserted = run(['git', 'commit-tree', f'{after}^{{tree}}', '-p', after, '-m', message],
                   cwd=workspace_dir).stdout[0]
    parent = inserted
    for commit in commits:
        raw = run(['git', 'cat-file', 'commit', commit],
                  cwd=workspace_dir, text=False).stdout
        # Bytes, so a message in another encoding is copied as it is.
        headers, _, body = raw.partition(b'\n\n')
        headers = re.sub(
            rb'^parent [0-9a-f]+$', f'parent {parent}'.encode(), headers, count=1, flags=re.MULTILINE)
        # A signature, with its indented lines, no longer matches the copy.
        headers = re.sub(
            rb'^gpgsig(-sha256)? [^\n]*(\n [^\n]*)*\n?', b'', headers, flags=re.MULTILINE).rstrip(b'\n')
        parent = run(['git', 'hash-object', '-t', 'commit', '-w', '--stdin'], cwd=workspace_dir,
                     input=headers + b'\n\n' + body, text=False).stdout.decode().strip()
    # HEAD moves the branch it is on, or itself when detached.
    run(['git', 'update-ref', 'HEAD', parent, head], cwd=workspace_dir)
    if not commits:
        return inserted, head, []
    current = get_current_branch(workspace_dir)
    result = run(['git', 'for-each-ref', f'--contains={commits[0]}', '--format=%(refname)', 'refs/heads/'],
                 cwd=workspace_dir)
    stale = [ref.removeprefix('refs/heads/') for ref in result.stdout]
    return inserted, head, [branch for branch in stale if branch != current]


def list_branches(workspace_dir: str) -> list[str]:
    """The local branches, by name."""
    return run(['git', 'for-each-ref', '--format=%(refname:short)', 'refs/heads/'],
               cwd=workspace_dir).stdout


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


def git_last_synced_changelist(workspace_dir: str, revision: str = 'HEAD') -> int | None:
    """Get the changelist number of the most recent sync commit in revision's history."""
    res = run(
        ['git', 'log', '-1', '--pretty=%s',
         f'--grep=^{_SYNC_SUBJECT_PREFIX}', revision, '--'],
        cwd=workspace_dir)
    if not res.stdout:
        return None
    return parse_sync_subject(res.stdout[0])


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
