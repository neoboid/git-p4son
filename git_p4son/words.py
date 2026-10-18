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
