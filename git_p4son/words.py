"""The words a backend's messages and completions use for its ideas."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Words:
    """A backend's words: its tool, what it calls a branch, and where a change's default message comes from."""
    tool: str
    branch: str
    message_source: str
