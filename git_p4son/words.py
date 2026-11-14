"""The words a backend's messages and completions use for its ideas."""

from collections.abc import Callable
from dataclasses import dataclass

from .common import CommandError


@dataclass(frozen=True)
class Words:
    """A backend's words: its tool, what it calls a branch, and where a change's default message comes from."""
    tool: str
    branch: str
    message_source: str


def named_base(base: str, words: Words, exists: Callable[[str], bool], names_at: Callable[[str], list[str]]) -> str:
    """The branch or bookmark base names, or the one at the commit it names; raise CommandError otherwise."""
    if exists(base):
        return base
    names = names_at(base)
    if len(names) != 1:
        at = f' Its commit has {", ".join(sorted(names))}.' if names else ''
        raise CommandError(
            f'-b {base} names no {words.branch}, and the CL records its base by name, as a commit goes stale after '
            f'a sync and rebase. Pass the {words.branch} the change is based on.{at}')
    return names[0]


def several_nearest(names: list[str], change: str, words: Words) -> CommandError:
    """The error for several branches or bookmarks on the nearest commits below change, for a base without -b."""
    kind = 'branches' if words.branch == 'branch' else f'{words.branch}s'
    return CommandError(f'-b is required, as {", ".join(names)} are the nearest {kind} below {change}. '
                        'Name the one it starts from.')


def has_merge(change: str, merge: str, sync_branch: str, words: Words) -> CommandError:
    """The error for a change with a merge commit, merge, above sync_branch."""
    # git's rebase replays a merge's commits in a line; jj's keeps the merge.
    fix = (f'Rebase it onto {sync_branch} instead of merging.' if words.tool == 'git' else
           f'Put its changes in a line on {sync_branch} instead: move the side that isn\'t in {sync_branch}\'s '
           'history onto the other parent with "jj rebase -s", then drop the parent left over with '
           '"jj simplify-parents".')
    return CommandError(f'{change} has a merge commit, {merge[:7]}, above {sync_branch}. A changelist is made from '
                        f'a straight line of commits. {fix}')


def sync_tip_heading(sync_branch: str, commits: list[str] | None) -> str:
    """The heading of the check that the change, or each of commits, contains the tip of sync_branch."""
    return f'Checking {"each commit " if commits else ""}for the latest {sync_branch}'


def not_on_sync_tip(change: str, sync_branch: str, words: Words) -> CommandError:
    """The error for a change, or a commit of it, that does not contain the tip of sync_branch."""
    return CommandError(
        f'{change} is not based on the latest {sync_branch}, which is what Perforce last synced the workspace to. '
        f'Rebase it onto {sync_branch}, or onto a {words.branch} that is rebased on it.')
