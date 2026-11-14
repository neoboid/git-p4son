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


def sync_tip_heading(sync_branch: str, commits: list[str] | None) -> str:
    """The heading of the check that the change, or each of commits, contains the tip of sync_branch."""
    return f'Checking {"each commit " if commits else ""}for the latest {sync_branch}'


def not_on_sync_tip(change: str, sync_branch: str, words: Words) -> CommandError:
    """The error for a change, or a commit of it, that does not contain the tip of sync_branch."""
    return CommandError(
        f'{change} is not based on the latest {sync_branch}, which is what Perforce last synced the workspace to. '
        f'Rebase it onto {sync_branch}, or onto a {words.branch} that is rebased on it.')
