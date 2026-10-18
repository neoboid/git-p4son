"""Runs a git p4son step on each commit since a base branch, with that commit checked out."""

import argparse
import shlex
import time
from collections.abc import Callable

from .backend import Backend
from .common import CommandError
from .git import get_commits_since, resolve_commit
from .log import log, plural

# Seconds to give Swarm to process a shelf before the next step shelves again.
SWARM_SHELF_DELAY = 5


def _parse(step: list[str], base: str, message: str | None, workspace_dir: str,
           backend: Backend, base_name: str | None) -> argparse.Namespace:
    """Parse a step's arguments with base and message, set up as run_command would, base_name as the base given."""
    # Imported here: cli imports the commands that import this module.
    from .cli import create_parser
    argv = [*step, '-b', base]
    if message is not None:
        # One argument: argparse would read a message starting with - after -m as an option.
        argv.append(f'--message={message}')
    args = create_parser().parse_args(argv)
    args.workspace_dir = workspace_dir
    args.invocation_dir = workspace_dir
    args.backend = backend
    args.base_given = base_name or base
    return args


def _resume_hint(rest: list[str], resume_after: str, message: str | None, message_file: str | None,
                 resumable: Callable[[], str | None] | None) -> str | None:
    """How to resume after resume_after, or None when resumable says there is nothing to resume."""
    note = resumable() if resumable else ''
    if note is None:
        return None
    message_args = []
    if message is not None:
        message_args = (['-F', message_file] if message_file
                        else [f'--message={message}'])
    command = shlex.join(['git', 'p4son', *rest, '--per-commit',
                          *message_args, '-b', resume_after])
    return f'{note} After fixing the problem, resume with: {command}'.lstrip()


def run_per_commit(first: list[str], rest: list[str] | None, base_branch: str, workspace_dir: str,
                   backend: Backend, *, sleep: int | None, dry_run: bool,
                   message: str | None = None, message_file: str | None = None,
                   resumable: Callable[[], str | None] | None = None, base_name: str | None = None) -> None:
    """Run the git p4son arguments first on the oldest commit since base_branch, rest (or first) on each later one.

    A failing step raises CommandError naming it, ending with the rest command that resumes after the last commit that
    worked, unless resumable returns None; any text it returns goes before that command. base_name, the name of the
    base, goes to every step, as the steps get the base as a commit."""
    rest = rest or first
    backend.check_clean()

    log.heading('Finding commits')
    base = resolve_commit(base_branch, workspace_dir)
    commits = get_commits_since(base, workspace_dir, backend.revision)
    if not commits:
        raise CommandError(f'No commits found since {base_branch}')
    log.success(f'{plural(len(commits), "commit")} since {base_branch}')

    steps = [first] + [rest] * (len(commits) - 1)
    if dry_run:
        log.heading('Planning steps')
        for (commit, subject), step in zip(commits, steps):
            log.info(
                f'{commit[:7]} {subject}: git p4son {shlex.join([*step, "-b", base])}')
        if message is not None:
            log.info('Message for the first step:')
            log.info(message)
        log.success(f'Would run {plural(len(steps), "step")}')
        return

    resume_after = base
    with backend.checkouts() as checkout:
        for number, ((commit, subject), step) in enumerate(zip(commits, steps), 1):
            name = f'{number} of {len(steps)}'
            log.heading(f'Step {name}: {commit[:7]} {subject}')
            checkout(commit)
            log.success(f'Checked out, running git p4son {shlex.join(step)}')
            step_message = message if number == 1 else None
            args = _parse(step, base, step_message,
                          workspace_dir, backend, base_name)
            try:
                args.func(args)
            except CommandError as error:
                hint = _resume_hint(rest, resume_after, step_message,
                                    message_file, resumable)
                raise CommandError(f'Step {name} failed at {commit[:7]} {subject}: {error}'
                                   + (f'\n{hint}' if hint else ''), error.returncode) from error
            resume_after = commit
            if sleep is not None and number < len(steps):
                log.heading(f'Sleeping for {sleep} seconds')
                time.sleep(sleep)
                log.success(f'Slept {sleep} seconds')
