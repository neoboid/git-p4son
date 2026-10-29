# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

git-p4son is a Python CLI tool that bridges Perforce (P4) and Git. It maintains a local git repository within a
Perforce workspace, keeping a `main` branch in sync with the Perforce depot while supporting feature branches for
local development.

## Development Commands

```bash
# Install in development mode, with the test, format and type-check tools
pip install -e . pytest pytest-xdist autopep8 mypy==2.4.0

# Point git at the repo's hooks: pre-commit checks autopep8, pre-push runs the tests and the type check
scripts/setup-hooks.sh

# Run the tool
git p4son [command] [options]
python -m git_p4son [command] [options]
```

The project has zero external dependencies (standard library only). Python 3.11+ is required.

Run tests with:
```bash
python -m pytest tests/ -n auto --maxprocesses 8
```

The tests are isolated in temporary directories, so they run in parallel with `pytest-xdist`; without it, drop the
`-n` options. The jj tests are skipped when `jj` is not on `PATH`, unless `GIT_P4SON_REQUIRE_JJ` is set, as CI sets
it, to make them fail instead. `scripts/install-jj.py VERSION DEST` downloads jj's official binary, as CI does.
`on_jj(TestClass)` in `tests/helpers.py` runs a command test class again on the jj backend, as a `JjTestCase`.
`tests/__init__.py` points `GIT_CONFIG_GLOBAL` at an empty config of the tests' own, with no global ignore or
attributes file, so a developer's global git settings don't reach git or jj in the tests. The system config still
applies, as Git for Windows sets `core.autocrlf` there.

Format code with `autopep8`:
```bash
autopep8 -i -r git_p4son/ tests/
```

**Important:** Before committing, always run `autopep8 -i -r git_p4son/ tests/` to format all changed code. The
project must stay PEP 8 compliant at all times.

Type-check `git_p4son/` and `tests/` with mypy in strict mode, once for Linux and once for Windows. Test functions may
leave out annotations, but their bodies are still checked, and the shared `tests/helpers.py` is held to full strict:
```bash
scripts/typecheck.sh
```

The settings are under `[tool.mypy]` in `pyproject.toml`. mypy is pinned to 2.4.0; bump it in a commit of its own.
Code that only type-checks on one platform, such as platform-only imports, goes under `sys.platform == 'win32'`
checks, which mypy narrows on; it does not narrow on `os.name`.

## Architecture

**`cli.py`** builds the argparse parser (`create_parser`), where each command registers its handler with
`set_defaults(func=...)` (and `needs_workspace=False` for commands that run outside a repo). `run_command` does the
shared setup before calling `args.func`: finding the workspace, resolving the `branch` keyword to an alias, reading
`-F` message files, deriving a message from HEAD, checking the workspace is clean once when the parser's
`needs_clean` says so (`check_clean_workspace`), and ending a dry run with "Dry run, nothing changed". `main` is the
only place that prints a `CommandError`/`RunError` and turns it into an exit code. `cli.py` also holds
`completion_command` and sends `complete` to `complete.py` before argparse runs.

The module-by-module map is in `ARCHITECTURE.md`. Read the part you are changing first, and update it with the
code.

## Python Version

When bumping the minimum Python version, update all of these locations:
- `pyproject.toml` (`requires-python`, classifiers and `[tool.mypy] python_version`)
- `README.md` (install requirements)
- `CLAUDE.md` (this file, development commands section)
- `.github/workflows/test.yml` (CI test matrix, which `publish.yml` runs before releasing)

## jj Version

CI runs the tests on the newest jj release and the oldest supported one, pinned as `JJ_LATEST` and `JJ_FLOOR` in
`.github/workflows/test.yml`. Bump `JJ_LATEST` when jj releases. When raising the minimum jj version, update all of
these together, with a CHANGELOG note:
- `git_p4son/jj.py` (`_MIN_VERSION`)
- `.github/workflows/test.yml` (`JJ_FLOOR`)
- `README.md` (the Jujutsu section)
- Once the floor reaches 0.42, `add_p4son_alias`'s plain-form branch (`_ALIAS_DOC_VERSION`), its test and the
  README's 0.41 alias sentence go

## Code Style

- Comments and docstrings must not obscure the code. Keep the commentary sparse; the code should carry the meaning.
- Docstrings are one line. Add a second short paragraph only for a contract the caller can't see from the signature,
  such as an exception raised or a special return value. No Args/Returns sections, no implementation walkthroughs.
- Write a `#` comment only for what the code cannot show: a data format, a platform quirk, or a non-obvious reason.
  Keep it to one line, two at most. Never restate the code or the `log.heading`/`log.error` next to it.
- Design rationale, history and the reasoning behind a fix belong in the commit message, not in comments.
- A fatal failure raises `CommandError` (from `common.py`), with any detail lines (file lists, names) logged with
  `log.info` just before. Only `cli.main` prints errors and turns them into exit codes, so functions do not log an
  error and return `None`/`False`/`1` for their callers to pass on. `log.error` elsewhere is only for failures the
  command reports and carries on past, such as a failing hook.

## Git Conventions

- Version bump commits should use the format: `Release git-p4son vX.Y.Z`. Releases go through a pull request, as
  `main` requires one. From an up-to-date `main`, `scripts/bump-version.py` (`patch|minor|major`) bumps the version
  and the changelog; `--finalize` commits them on a `release-vX.Y.Z` branch and opens the pull request; once it is
  merged, `--publish` tags the merged commit on `main`, which starts the PyPI publish, and creates the GitHub
  release.
- Never commit plan/analysis markdown files (e.g. `release-notes.md`, `default-message.md`) together with the
  implementation changes. These files are a communication tool between the user and Claude, not part of the codebase.
