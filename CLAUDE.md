# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

git-p4son is a Python CLI tool that bridges Perforce (P4) and Git. It maintains a local git repository within a Perforce workspace, keeping a `main` branch in sync with the Perforce depot while supporting feature branches for local development.

## Development Commands

```bash
# Install in development mode
pip install -e .

# Run the tool
git-p4son [command] [options]
python -m git_p4son [command] [options]
```

There are no tests, linters, or CI configured. The project has zero external dependencies (standard library only). Python 3.7+ is required.

Format code with `autopep8`:
```bash
autopep8 --in-place <file>
```

## Architecture

The CLI (`cli.py`) dispatches to three command modules, each exposing a `*_command(args)` entry point:

- **`sync.py`** — Syncs git repo with a Perforce changelist. Validates both git and p4 workspaces are clean, performs `p4 sync`, then creates a git commit. Supports syncing to a specific CL number, `latest`, or `last-synced`. Uses threaded real-time output processing (`P4SyncOutputProcessor`) to parse p4 sync progress.

- **`edit.py`** — Opens git-changed files for edit in Perforce. Computes changes between a base branch and HEAD using `git merge-base` for common ancestor detection, then maps git operations (add/modify/delete/rename) to corresponding p4 operations (edit/add/delete/move). Supports creating new changelists with git commit descriptions.

- **`list_changes.py`** — Lists git commit subjects since a base branch in chronological order. Used for generating changelist descriptions.

**`common.py`** provides shared utilities: workspace detection (walks up directory tree for `.git`), subprocess execution with timing (`run()`), and real-time output streaming via threading (`run_with_output()`).

## Git Conventions

- Never mention "Claude" or "Co-Authored-By: Claude" in commit messages.
