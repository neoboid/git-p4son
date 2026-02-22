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

The project has zero external dependencies (standard library only). Python 3.10+ is required.

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

- **`sync.py`** — Syncs git repo with a Perforce changelist. Validates both git and p4 workspaces are clean, performs `p4 sync`, then creates a git commit. Supports syncing to a specific CL number, `latest`, or `last-synced`. Uses threaded real-time output processing (`P4SyncOutputProcessor`) to parse p4 sync progress.

- **`new.py`** — Creates a new Perforce changelist, opens git-changed files for edit, and optionally creates a Swarm review (with `--review` flag) or shelves (with `--shelve` flag).

- **`update.py`** — Updates an existing changelist description, opens git-changed files for edit, and optionally re-shelves (with `--shelve` flag).

- **`list_changes.py`** — Lists git commit subjects since a base branch in chronological order. Used for generating changelist descriptions.

**`lib.py`** contains all reusable Perforce/git library functions: changelist creation/update, file status checking, opening files for edit, shelving, and Swarm review keyword management.

**`changelist_store.py`** provides changelist alias utilities, storing named aliases for changelist numbers in `.git-p4son/changelists/<name>`.

**`common.py`** provides shared utilities: workspace detection (walks up directory tree for `.git`), subprocess execution with timing (`run()`), and real-time output streaming via threading (`run_with_output()`).

## Git Conventions

- Never mention "Claude" or "Co-Authored-By: Claude" in commit messages.
