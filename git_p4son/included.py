"""The commits a changelist includes: the record new saves with its alias, and the update planned from it."""

from collections import Counter
from dataclasses import dataclass

from .backend import Backend
from .changelist_store import Record
from .common import CommandError
from .git import (
    changed_paths, commit_subject, get_commits_since, git_last_synced_changelist, merge_base, resolve_commit,
)
from .log import plural


# A dropped commit's slot in the record, which no identity matches, so positions stay the CL's entry numbers.
DROPPED = ''


def included_count(record: Record) -> int:
    """How many commits the record says its CL includes, leaving out dropped ones."""
    return sum(identity != DROPPED for identity in record.included)


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
    # The 1-based positions, among the commits the CL included, of those gone from the branch.
    dropped: list[int]
    # The first new commit when it is the record's catch-up, which is shelved on its own.
    catch_up: str | None = None

    @property
    def kept(self) -> Record:
        """The record of the commits the CL still includes, before this update adds any."""
        still = self.next.included[:len(
            self.next.included) - len(self.subjects)]
        return Record(self.current.base, self.current.base_commit, still, self.current.catch_up)


def new_record(base: str, base_commit: str, revision: str, shelved: bool, backend: Backend,
               workspace_dir: str) -> Record:
    """The record of a CL that includes every commit from base_commit to revision, its shelf on it if shelved."""
    commits = get_commits_since(base_commit, workspace_dir, revision)
    on = merge_base(base_commit, revision, workspace_dir) if shelved else ''
    return Record(base, on, backend.commit_identities([sha for sha, _ in commits]))


def catch_up_subject(plan: Plan, workspace_dir: str, revision: str) -> str | None:
    """The subject of the empty commit that gives a moved base its own patch, or None if the CL's shelf needs none.

    It needs one when the base the branch is on moved since the last shelve and changed a file the change touches."""
    shelved, base = plan.current.base_commit, plan.next.base_commit
    if not shelved or shelved == base or not included_count(plan.kept):
        return None
    if not changed_paths(shelved, base, workspace_dir) & changed_paths(base, revision, workspace_dir):
        return None
    changelist = git_last_synced_changelist(workspace_dir, base)
    return 'Rebase' if changelist is None else f'Rebase onto CL {changelist}'


def plan_update(record: Record, alias: str, changelist: str, given: tuple[str, str] | None, backend: Backend,
                workspace_dir: str, revision: str) -> Plan:
    """Plan an update of changelist through revision, starting at given, the -b as typed and resolved, if not None.

    Raises CommandError if a commit the CL doesn't include sits below ones it does, or given skips commits it doesn't
    include."""
    base_commit = resolve_commit(
        backend.resolve_base(record.base), workspace_dir)
    commits = get_commits_since(base_commit, workspace_dir, revision)
    shas = [sha for sha, _ in commits]
    identities = backend.commit_identities(shas)
    # Counted, as two git commits made in the same second share an author date.
    unmatched = Counter(
        identity for identity in record.included if identity != DROPPED)
    first_new: int | None = None
    for index, identity in enumerate(identities):
        if unmatched[identity] == 0:
            first_new = index if first_new is None else first_new
        elif first_new is not None:
            sha, subject = commits[first_new]
            raise CommandError(
                f'"{commit_subject(sha, subject)}" sits below commits CL {changelist} already includes, so it can\'t '
                f'be shelved as a patch of its own. Move it after them, then update again.')
        else:
            unmatched[identity] -= 1
    included = len(identities) if first_new is None else first_new
    after = shas[included - 1] if included else base_commit
    if given is None:
        # With nothing new, every file since the base, as an amend keeps a commit's identity but can add files.
        start = after if included < len(shas) else base_commit
    elif (position := _position(resolve_commit(given[1], workspace_dir), shas)) <= included:
        start = given[1]
    else:
        raise CommandError(
            f'-b {given[0]} skips {plural(position - included, "commit")} CL {changelist} doesn\'t include yet, which '
            f'would be counted without being opened. Start at or below commit {included} since {record.base}.')
    dropped = _dropped(record.included, unmatched)
    # The base the branch is on, which the shelf follows: a sync moves the base before the branch is rebased.
    on = merge_base(base_commit, revision, workspace_dir)
    slots = [DROPPED if position in dropped else identity for position,
             identity in enumerate(record.included, 1)]
    catch_up = shas[included] if included < len(
        shas) and record.catch_up == identities[included] else None
    return Plan(alias, record, Record(record.base, on, slots + identities[included:]), start, after,
                [commit_subject(sha, subject) for sha, subject in commits[included:]], dropped, catch_up)


def _dropped(included: list[str], unmatched: Counter[str]) -> list[int]:
    """The 1-based positions in included of the identities left unmatched, the first of any repeated ones."""
    left = unmatched.copy()
    positions = []
    for position, identity in enumerate(included, 1):
        if identity != DROPPED and left[identity] > 0:
            left[identity] -= 1
            positions.append(position)
    return positions


def _position(commit: str, shas: list[str]) -> int:
    """Commit's 1-based position among the commits since the base, 0 at or below it."""
    return shas.index(commit) + 1 if commit in shas else 0
