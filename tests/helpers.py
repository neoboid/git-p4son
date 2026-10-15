"""Shared test helpers for git_p4son tests."""

from git_p4son.common import RunResult
from git_p4son.perforce import P4Change, P4SyncPreviewFile


def make_run_result(returncode=0, stdout=None, stderr=None, elapsed=None):
    """Build a RunResult for mocking run()."""
    return RunResult(
        returncode=returncode,
        stdout=stdout if stdout is not None else [],
        stderr=stderr if stderr is not None else [],
        elapsed=elapsed,
    )


def preview_update(path):
    """Preview entry for a file p4 would update."""
    return P4SyncPreviewFile(mode='upd', filepath=path)


def make_changes(*pairs):
    """Build a change list from (changelist, user) pairs."""
    return [P4Change(change=cl, user=user) for cl, user in pairs]


class MockRunDispatcher:
    """Answer run() calls with the result of the first command-prefix match, else the default."""

    def __init__(self, mapping=None, default=None):
        self.mapping = mapping or {}
        self.default = default or make_run_result(
            returncode=1, stderr=['unmatched command'])
        self.calls = []

    def __call__(self, command, cwd='.', dry_run=False, input=None,
                 env=None):
        self.calls.append((command, cwd, dry_run))
        for prefix, result in self.mapping.items():
            if tuple(command[:len(prefix)]) == prefix:
                return result
        return self.default


# A pending changelist spec as p4 change -o prints it.
SAMPLE_SPEC = """\
# A Perforce Change Specification.
Change:\t12345

Client:\tmyclient

User:\tmyuser

Status:\tpending

Description:
\tFix the login bug
\t1. Add validation
\t2. Fix redirect

Files:
\t//depot/src/login.py\t# edit
"""

SAMPLE_SPEC_NO_COMMITS = """\
Change:\tnew

Description:
\tJust a message

Files:
"""
