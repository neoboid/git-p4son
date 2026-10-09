# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

git-p4son is a Python CLI tool that bridges Perforce (P4) and Git. It maintains a local git repository within a Perforce workspace, keeping a `main` branch in sync with the Perforce depot while supporting feature branches for local development.

## Development Commands

```bash
# Install in development mode
pip install -e .

# Run the tool
git p4son [command] [options]
python -m git_p4son [command] [options]
```

The project has zero external dependencies (standard library only). Python 3.11+ is required.

Run tests with:
```bash
python -m pytest tests/
```

Format code with `autopep8`:
```bash
autopep8 -i -r git_p4son/ tests/
```

**Important:** Before committing, always run `autopep8 -i -r git_p4son/ tests/` to format all changed code. The
project must stay PEP 8 compliant at all times.

## Architecture

The CLI (`cli.py`) dispatches to command modules, each exposing a `*_command(args)` entry point:

- **`sync.py`** — Syncs git repo with a Perforce changelist. Validates both git and p4 workspaces are clean, performs
  `p4 sync`, then creates a git commit. Accepts one or more explicit CL numbers, `last-synced`, or `head`; omitting the
  argument syncs to the latest changelist. Multiple strictly increasing CL numbers are synced in sequence with one
  commit each (a trailing `head` may close out the sequence). Refuses to start while a configured blocking process
  (see `processes.py`) is running, unless `--ignore-blocking-processes` is given. Changelist arguments are
  numeric-only: `sync` does not resolve changelist aliases (those name pending CLs from `new`/`review`, which Perforce
  renumbers on submit), only submitted CL numbers are meaningful. Uses threaded real-time output processing
  (`P4SyncOutputProcessor`) to parse p4 sync progress. When moving forward from a previous sync, also syncs the
  changelist before each of the split users' submits (`build_sync_targets`), so each of their changelists gets a
  commit of its own; `-u` adds users for one run and `--no-split` ignores the configured ones.

- **`sync_split_users.py`** - The split users: reading and writing the `sync.split-users` list, resolving the
  `$(user)` placeholder to the current Perforce user, and the `sync-split-users` command (`list`, `add`, `delete`).

- **`new.py`** — Creates a new Perforce changelist, opens git-changed files for edit, reverts files that are no
  longer part of the git change, and optionally creates a Swarm review (with `--review` flag) or shelves (with
  `--shelve` flag). Requires a clean git workspace unless `--no-edit` is given. Alias defaults to the current branch
  name; use `--no-alias` to skip alias creation.

- **`update.py`** — Updates an existing changelist description, opens git-changed files for edit, reverts files that
  are no longer part of the git change, and optionally re-shelves (with `--shelve` flag). Requires a clean git
  workspace unless `--no-edit` is given.

- **`list_changes.py`** — Lists git commit subjects since a base branch in chronological order. Used for generating changelist descriptions.

**`lib.py`** contains all reusable Perforce/git library functions: changelist creation/update, file status checking,
opening files for edit, reverting files no longer part of the git change (`revert_stale_files`), shelving, and Swarm
review keyword management.

**`changelist_store.py`** provides changelist alias utilities, storing named aliases for changelist numbers in
`.git-p4son/changelists/<name>`.

**`config.py`** manages per-repo configuration stored in `.git-p4son/config.toml`, such as the depot root (the
Perforce path to sync, e.g. `//my-workspace` or `//my-workspace/Engine/Source`) and the split users.

**`depot.py`** owns the depot root: reading it from config, expanding the `$(workspace)` placeholder, and resolving it
against the client spec for the commands that run Perforce queries against it (`sync`).

**`writable.py`** implements writable mode: reading and writing the `core.writable` setting, the `writable` command,
and the helpers that set and clear the user write bit on git-tracked files, which `sync` also uses.

**`processes.py`** implements blocking processes: reading the `[sync] blocking-processes` setting, listing running
processes (`ps` or `tasklist`), and the check `sync_preflight` runs first to refuse a sync while one of them runs.

**`skill.py`** implements the `skill` command: `install` writes a stub `SKILL.md` to the user's Claude Code skills
directory, and `show` prints the full agent instructions shipped in `skill/git-p4son.md`. The stub only tells the agent
to run `skill show`, so the instructions follow the installed version. `init` offers the install.

**`common.py`** provides shared utilities: workspace detection (walks up directory tree for `.git`), subprocess execution
with timing (`run()`), and real-time output streaming via threading (`run_with_output()`).

## Python Version

When bumping the minimum Python version, update all of these locations:
- `pyproject.toml` (`requires-python` and classifiers)
- `README.md` (install requirements)
- `CLAUDE.md` (this file, development commands section)
- `.github/workflows/publish.yml` (CI test matrix)

## Code Style

- Comments and docstrings must not obscure the code. Keep the commentary sparse; the code should carry the meaning.
- Docstrings are one line. Add a second short paragraph only for a contract the caller can't see from the signature,
  such as an exception raised or a special return value. No Args/Returns sections, no implementation walkthroughs.
- Write a `#` comment only for what the code cannot show: a data format, a platform quirk, or a non-obvious reason.
  Keep it to one line, two at most. Never restate the code or the `log.heading`/`log.error` next to it.
- Design rationale, history and the reasoning behind a fix belong in the commit message, not in comments.

## Git Conventions

- Version bump commits should use the format: `Release git-p4son vX.Y.Z`
- Never commit plan/analysis markdown files (e.g. `release-notes.md`, `default-message.md`) together with the
  implementation changes. These files are a communication tool between the user and Claude, not part of the codebase.
