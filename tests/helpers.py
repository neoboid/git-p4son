"""Shared test helpers for git_p4son tests."""

from git_p4son.common import RunResult


def make_run_result(returncode=0, stdout=None, stderr=None):
    """Factory for RunResult objects.

    Args:
        returncode: Process return code (default 0).
        stdout: List of stdout lines (default empty).
        stderr: List of stderr lines (default empty).
    """
    return RunResult(
        returncode=returncode,
        stdout=stdout if stdout is not None else [],
        stderr=stderr if stderr is not None else [],
    )


class MockRunDispatcher:
    """Maps command prefixes to RunResult responses.

    Usage:
        dispatcher = MockRunDispatcher({
            ('p4', 'info'): make_run_result(stdout=['Client name: myclient']),
            ('git', 'status'): make_run_result(stdout=[]),
        })
        with mock.patch('git_p4son.common.run', side_effect=dispatcher):
            ...

    Commands are matched by tuple prefix: ('p4', 'info') matches
    ['p4', 'info', ...] regardless of extra arguments.

    A default result can be provided for unmatched commands.
    """

    def __init__(self, mapping=None, default=None):
        self.mapping = mapping or {}
        self.default = default or make_run_result(
            returncode=1, stderr=['unmatched command'])
        self.calls = []

    def __call__(self, command, cwd='.', dry_run=False, input=None):
        self.calls.append((command, cwd, dry_run))
        for prefix, result in self.mapping.items():
            if tuple(command[:len(prefix)]) == prefix:
                return result
        return self.default
