"""The commits a changelist includes: the record new saves with its alias, and the update planned from it."""

from dataclasses import dataclass

from .backend import Backend
from .changelist_store import Record
from .common import CommandError
from .git import commit_subject, get_commits_since, resolve_commit
from .log import plural


@dataclass(frozen=True)
class Plan:
    """What update does for a CL whose alias holds a record."""
    alias: str
    # The record as it stands, and as it will once the update is done.
    current: Record
    next: Record
    # The base for the files to open and the per-commit loop.
    start: str
    # The last commit the CL includes, or the base's commit if none, where the commits it doesn't include start.
    after: str
    # The commit list entries to append, for the commits the CL doesn't include yet.
    subjects: list[str]


def _record(base: str, base_commit: str, commits: list[tuple[str, str]], backend: Backend) -> Record:
    last = backend.commit_identity(commits[-1][0]) if commits else ''
    return Record(base, base_commit, len(commits), last)


def new_record(base: str, base_commit: str, revision: str, backend: Backend, workspace_dir: str) -> Record:
    """The record of a CL that includes every commit from base_commit to revision."""
    return _record(base, base_commit, get_commits_since(base_commit, workspace_dir, revision), backend)


def plan_update(record: Record, alias: str, changelist: str, given: tuple[str, str] | None, backend: Backend,
                workspace_dir: str, revision: str) -> Plan:
    """Plan an update of changelist through revision, starting at given, the -b as typed and resolved, if not None.

    Raises CommandError if the commits the CL includes changed since, or given skips commits it doesn't include."""
    base_commit = resolve_commit(
        backend.resolve_base(record.base), workspace_dir)
    commits = get_commits_since(base_commit, workspace_dir, revision)
    included = record.included
    if included > len(commits) or (included and backend.commit_identity(commits[included - 1][0]) != record.last):
        raise CommandError(
            f'The commits CL {changelist} includes changed since its last update: commit {included} since '
            f'{record.base} is not the one it included. Put them back, or drop the record with '
            f'"git p4son alias new {changelist} {alias} --force" and pass -b.')
    shas = [sha for sha, _ in commits]
    after = shas[included - 1] if included else base_commit
    if given is None:
        start = after
    elif (position := _position(resolve_commit(given[1], workspace_dir), shas)) <= included:
        start = given[1]
    else:
        raise CommandError(
            f'-b {given[0]} skips {plural(position - included, "commit")} CL {changelist} doesn\'t include yet, which '
            f'would be counted without being opened. Start at or below commit {included} since {record.base}.')
    subjects = [commit_subject(sha, subject)
                for sha, subject in commits[included:]]
    return Plan(alias, record, _record(record.base, base_commit, commits, backend), start, after, subjects)


def _position(commit: str, shas: list[str]) -> int:
    """Commit's 1-based position among the commits since the base, 0 at or below it."""
    return shas.index(commit) + 1 if commit in shas else 0
