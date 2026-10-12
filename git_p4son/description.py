"""Changelist descriptions: a message, the enumerated commit list and Swarm's review keywords."""

import re
from collections import Counter
from dataclasses import dataclass, field

# Heading written above the enumerated commit list in descriptions.
COMMIT_LIST_MARKER = 'Changes included:'

# #review on a line of its own, which Swarm turns into #review-<id>.
_REVIEW_KEYWORD_RE = re.compile(r'^#review(-\d+)?$')


def _is_review_keyword(line: str) -> bool:
    return bool(_REVIEW_KEYWORD_RE.match(line.strip()))


def _strip_trailing_blank_lines(lines: list[str]) -> list[str]:
    end = len(lines)
    while end and not lines[end - 1].strip():
        end -= 1
    return lines[:end]


def _commit_list_start(lines: list[str]) -> int | None:
    """Return the index of the commit list's first line, or None if there is no list."""
    for i, line in enumerate(lines):
        if line.strip() == COMMIT_LIST_MARKER:
            following = next((j for j in range(i + 1, len(lines))
                              if lines[j].strip()), None)
            if following is not None and lines[following].startswith('1. '):
                return following
    # Fallback for descriptions written without the marker.
    return next((i for i, line in enumerate(lines) if line.startswith('1. ')),
                None)


def number_commits(subjects: list[str]) -> list[str]:
    """Return the subjects as an enumerated list, "1. <subject>" first."""
    return [f'{number}. {subject}' for number, subject in enumerate(subjects, 1)]


@dataclass
class Description:
    """A changelist description; format() always writes the commit list after the message and keywords last."""

    message: list[str] = field(default_factory=list)
    commits: list[str] = field(default_factory=list)
    trailing: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)

    @classmethod
    def parse(cls, lines: list[str]) -> 'Description':
        """Split description lines into their parts; review keywords on lines of their own are taken out."""
        start = _commit_list_start(lines)
        if start is None:
            message, numbered, trailing = lines, [], []
        else:
            # The list runs on while the lines keep counting "2. ", "3. ", ...
            end = start + 1
            while end < len(lines) and lines[end].startswith(f'{end - start + 1}. '):
                end += 1
            numbered = lines[start:end]
            trailing = lines[end:]
            message = _strip_trailing_blank_lines(lines[:start])
            if message and message[-1].strip() == COMMIT_LIST_MARKER:
                message = message[:-1]

        description = cls(
            commits=[re.sub(r'^\d+\. ', '', line) for line in numbered])
        description.message = description._take_keywords(message)
        description.trailing = description._take_keywords(trailing)
        return description

    def _take_keywords(self, lines: list[str]) -> list[str]:
        """Move the review keyword lines into keywords, returning the other lines."""
        others = []
        for line in lines:
            if _is_review_keyword(line):
                self.add_keyword(line.strip())
            else:
                others.append(line)
        return _strip_trailing_blank_lines(others)

    def set_message(self, message: str) -> None:
        """Replace the message; a review keyword in it joins the keywords."""
        self.message = self._take_keywords(message.splitlines())

    def add_keyword(self, keyword: str) -> None:
        if keyword not in self.keywords:
            self.keywords.append(keyword)

    def merge_commits(self, subjects: list[str]) -> None:
        """Replace the listed commits the subjects cover and append the subjects after the others."""
        # Counted, not a set, so repeated subjects replace one-for-one.
        replaced = Counter(subjects)
        kept = []
        for subject in self.commits:
            if replaced[subject] > 0:
                replaced[subject] -= 1
            else:
                kept.append(subject)
        self.commits = kept + subjects

    def format(self) -> list[str]:
        lines = list(self.message)
        if self.commits:
            lines += ['', COMMIT_LIST_MARKER] + number_commits(self.commits)
        lines += self.trailing
        if self.keywords:
            lines += [''] + self.keywords
        return lines
