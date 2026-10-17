# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

git-p4son is a Python CLI tool that bridges Perforce (P4) and Git. It maintains a local git repository within a
Perforce workspace, keeping a `main` branch in sync with the Perforce depot while supporting feature branches for
local development.

## Development Commands

```bash
# Install in development mode, with the test and format tools
pip install -e . pytest pytest-xdist autopep8

# Point git at the repo's hooks: pre-commit checks autopep8, pre-push runs the tests
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
`-n` options.

Format code with `autopep8`:
```bash
autopep8 -i -r git_p4son/ tests/
```

**Important:** Before committing, always run `autopep8 -i -r git_p4son/ tests/` to format all changed code. The
project must stay PEP 8 compliant at all times.

## Architecture

**`cli.py`** builds the argparse parser (`create_parser`), where each command registers its handler with
`set_defaults(func=...)` (and `needs_workspace=False` for commands that run outside a repo). `run_command` does the
shared setup before calling `args.func`: finding the workspace, resolving the `branch` keyword to an alias, reading
`-F` message files, deriving a message from HEAD, and ending a dry run with "Dry run, nothing changed". `main` is the
only place that prints a `CommandError`/`RunError` and turns it into an exit code. `cli.py` also holds
`completion_command` and sends `complete` to `complete.py` before argparse runs.

### Commands

- **`sync.py`** - Syncs the git repo with Perforce, one git commit per changelist. `_plan_sync` resolves the
  arguments (one or more strictly increasing CL numbers, `last-synced`, or a trailing `head`; none means the latest)
  into a `_SyncPlan`, or None when already synced. `sync_preflight` refuses to start while a blocking process runs
  (unless `--ignore-blocking-processes`), while either workspace is dirty, or when a pre-sync hook fails. `_run_sync`
  runs a catch-up pass to the last synced CL, syncs and commits each target, merges local changes back once at the
  end (`sync_merge.py`) and runs the post-sync hooks. Changelist arguments are numeric-only: aliases name pending
  CLs, which Perforce renumbers on submit. When moving forward, `build_sync_targets` also syncs the CL before each
  split user's submit, so each of their changelists gets a commit of its own; `-u` adds users for one run and
  `--no-split` ignores the configured ones.

- **`sync_split_users.py`** - The split users: reading and writing the `sync.split-users` list, resolving the
  `$(user)` placeholder to the current Perforce user (`resolve_split_users`), and the `sync-split-users` command
  (`list`, `add`, `delete`).

- **`new.py`** - Creates a Perforce changelist from the git change (`create_changelist`) and saves an alias for it
  (the branch name by default; `--no-alias` skips it), then opens the changes and reverts the files no longer part of
  them through `edit_and_shelve`. `--review` adds a `#review` keyword and shelves; `--shelve` only shelves. Requires a
  clean git workspace unless `--no-edit` is given.

- **`update.py`** - Updates an existing changelist, named by number or alias: merges the commits into its
  description (`update_changelist`), then calls `edit_and_shelve`. `--per-commit` instead updates once per commit
  through `run_per_commit`. Requires a clean git workspace unless `--no-edit` is given.

- **`review.py`** - The `review` command: one Swarm review built commit by commit through `run_per_commit`,
  running `new <alias> --review` on the first commit and `update <alias> --shelve` on the others.

- **`list_changes.py`** - The `list-changes` command: prints the numbered commit subjects since a base branch.

- **`alias.py`** - The `alias` actions (`list`, `show`, `new`, `delete`, `clean`), each an `alias_*_command` built on
  `changelist_store.py`; `clean` and `delete --all` go on past an alias that fails to delete.

- **`init.py`** - The `init` command: checks it runs in a Perforce workspace, then asks for the depot root, writable
  mode, split users and the Claude Code skill. It sets up `.gitignore` (from `.p4ignore` if present), creates the git
  repo with a first commit, and checks for a git editor.

- **`writable.py`** - Writable mode: reading and writing the `core.writable` setting and the `writable` command
  (shows the mode without an action; `enable`, `disable`, `apply`). Its helpers (`make_writable`, `make_read_only`,
  `restore_writable`, `is_user_writable`) set and clear the user write bit on git-tracked files and are also used by
  `sync` and `lib`. Making files read-only skips files opened in Perforce, and allwrite workspaces entirely.

- **`skill.py`** - The `skill` command: `install` writes a stub `SKILL.md` to the user's Claude Code skills directory,
  and `show` prints the full agent instructions shipped in `skill/git-p4son.md`, followed by the workspace's own
  overrides in `.git-p4son/skill-override.md` when run inside a workspace that has one. The stub only tells the agent
  to run `skill show`, so the instructions follow the installed version. `init` offers the install.

### Command support

- **`sync_merge.py`** - Saves what a sync would overwrite and merges it back: `prepare_writable_files` sorts the
  writable files p4 would touch into tracked and ignored ones and stages the locally modified ones, and
  `merge_changed_files` merges those local changes back after the sync. `report_not_synced` lists the git-ignored
  writable files p4 refused or overwrote.

- **`per_commit.py`** - `run_per_commit` runs a git p4son command on each commit since a base branch, in-process,
  with that commit checked out through the backend, for `review` and `update --per-commit`. A step's arguments are
  parsed with the real parser just before it runs, with the base resolved once to a SHA. It returns to where it
  started, also when a step fails, ends the error with the command that resumes after the last commit that worked,
  and sleeps `SWARM_SHELF_DELAY` between shelving steps.

- **`processes.py`** - Blocking processes: reading the `[sync] blocking-processes` setting, listing running
  processes (`ps` or `tasklist`), and `check_no_blocking_processes`, which `sync_preflight` runs first.

- **`hooks.py`** - User hooks: `run_hooks` runs every executable file in `.git-p4son/hooks/<name>/` in name order,
  from the invocation directory with `GIT_P4SON_REPO_ROOT_DIR` set, without stopping at a failure. On Windows,
  scripts run through extension associations, overridable with `[hooks.extension-associations]`. `sync` runs the
  `pre-sync` and `post-sync` hooks.

- **`complete.py`** - Shell completion: `run_complete` walks the argparse parser to suggest commands, flags, actions,
  aliases, split users and the `branch` keyword. Logging is silenced so stdout carries only candidates; the scripts in
  `completions/` call `git p4son complete`.

- **`jj.py`** - Colocated jj workspaces. `operation` pins a jj operation and holds jj's working-copy lock while a
  command runs, yielding a `Workspace` (or None outside jj, or with `--ignore-jj`) that `run_command` stores as
  `args.jj_workspace`. The `Workspace` resolves the change git-p4son works on (the working change, or its parent
  when the working change is empty and new), its bookmark and the default base, and `change_revision` gives the
  commit the git functions read the change from. git still reads objects, diffs and makes the sync commits; `sync`
  moves the selected bookmark itself. With a revision, `lib.py` keeps the working files around Perforce reverts
  instead of restoring git's index version.

- **`jj_lock.py`** - Takes jj's local working-copy lock the way jj does, with `flock` on POSIX and `LockFileEx` on
  Windows. A busy lock raises `CommandError` instead of waiting. Hooks must not run jj commands that snapshot, and
  a git-p4son run under its caller's lock needs `--ignore-jj`.

### Shared modules

- **`backend.py`** - The backend: what a command asks of the tool holding the change, as the `Backend`
  interface. `open_backend` yields it in `run_command` as `args.backend`; so far always a `GitBackend`.

- **`git_backend.py`** - `GitBackend`, the git adapter: `checkouts` detaches at each commit and returns to the
  starting branch.

- **`lib.py`** - Combines git and Perforce operations: `check_git_workspace_clean`, `create_changelist`,
  `update_changelist`, `include_changes_in_changelist` (returns how many files it opened or moved),
  `revert_unchanged_files`, and `edit_and_shelve`, which `new` and `update` share. On a dry run of `new` the
  changelist is `DRY_RUN_CHANGELIST`.

- **`description.py`** - Models a changelist description (`Description`): the message, the enumerated commit list
  under `Changes included:`, trailing text, and Swarm `#review` keywords on lines of their own. `parse` and `format`
  own the layout; `new` and `update` build descriptions through it.

- **`changelist_store.py`** - Changelist aliases stored in `.git-p4son/changelists/<name>`: save, load, list and
  delete, name validation, the reserved keywords (`last-synced`, `head`, `branch`) and `branch_alias` for the branch
  keyword.

- **`depot.py`** - Owns the depot root: reading it from config, expanding the `$(workspace)` placeholder, and
  `resolve_depot_root`, which returns it with the client spec (`ResolvedDepot`) for `sync`. `check_depot_root` and
  `subdirectory_depot_root` back `init`'s choice.

- **`config.py`** - Per-repo configuration in `.git-p4son/config.toml`. `get_setting` reads a typed value with a
  default and raises `ConfigError` for a wrong type or invalid TOML; `save_config` merges into the file. Holds the
  depot root, `core.writable`, the split users, the blocking processes and the hook associations.

- **`git.py`** - The git layer: workspace discovery, dirty and staged files, `get_local_changes` (adds, edits,
  deletes and moves since a base), commit subjects, staging and commits, the sync commit subject
  (`format_sync_subject`, `git_last_synced_changelist`), blob reads and `merge_file` for `sync_merge`, and
  `resolve_editor`. git's `-z` output is read as bytes.

- **`perforce.py`** - The Perforce layer, with every p4 call pinned to English output (`-L en`): ztag parsing, the
  client spec (`P4ClientSpec`, `ClobberMode`), changelist specs, opened files and batched commands (`run_p4_batch`),
  reverts, shelving, submitted changes and users, and `p4_sync`/`p4_sync_preview` with `P4SyncOutputProcessor`
  parsing streamed progress and raising `P4SyncClobberError` or `P4SyncAbortError`.

- **`common.py`** - Shared utilities: the `CommandError` and `RunError` exceptions, subprocess execution (`run()`,
  `RunResult`), real-time output streaming via threading (`run_with_stream_output()`, used by `p4 sync`),
  `normalize_workspace_path`, and the interactive prompts `prompt_choice` and `prompt_menu`.

- **`log.py`** - All user-facing output goes through the `log` singleton: headings, success, warning and error lines,
  file changes, command echo with a spinner, child process output, and quiet and verbose modes. `plural` formats
  counts.

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
- A fatal failure raises `CommandError` (from `common.py`), with any detail lines (file lists, names) logged with
  `log.info` just before. Only `cli.main` prints errors and turns them into exit codes, so functions do not log an
  error and return `None`/`False`/`1` for their callers to pass on. `log.error` elsewhere is only for failures the
  command reports and carries on past, such as a failing hook.

## Git Conventions

- Version bump commits should use the format: `Release git-p4son vX.Y.Z`. `scripts/bump-version.py`
  (`patch|minor|major`, then `--finalize`) makes that commit, the tag and the GitHub release.
- Never commit plan/analysis markdown files (e.g. `release-notes.md`, `default-message.md`) together with the
  implementation changes. These files are a communication tool between the user and Claude, not part of the codebase.
