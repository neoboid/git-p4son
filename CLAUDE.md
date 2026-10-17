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
`-F` message files, deriving a message from HEAD, and ending a dry run with "Dry run, nothing changed". `main` is the
only place that prints a `CommandError`/`RunError` and turns it into an exit code. `cli.py` also holds
`completion_command` and sends `complete` to `complete.py` before argparse runs.

### Commands

- **`sync.py`** - Syncs the git repo with Perforce, one git commit per changelist, onto the sync branch from any
  branch. `_plan_sync` resolves the arguments (one or more strictly increasing CL numbers, `last-synced`, or a
  trailing `head`; none means the latest) into a `_SyncPlan`, reading the last sync from the sync branch the
  backend's `prepare_sync` chose, or None when already synced. `sync_preflight` refuses to start while a blocking
  process runs (unless `--ignore-blocking-processes`), or while either workspace is dirty, the Perforce one judged
  by the sync branch's files. `sync_command` then has the backend check out the sync branch (`start_sync`), and
  returns (`abandon_sync`) if a pre-sync hook or a split query fails. `_run_sync`
  runs a catch-up pass to the last synced CL, syncs and commits each target, merges local changes back once at the
  end (`sync_merge.py`) and runs the post-sync hooks. Changelist arguments are numeric-only: aliases name pending
  CLs, which Perforce renumbers on submit. When moving forward, `build_sync_targets` also syncs the CL before each
  split user's submit, so each of their changelists gets a commit of its own; `-u` adds users for one run and
  `--no-split` ignores the configured ones. `finish_sync` returns to where sync started, unless changes are left
  in the work tree, as merged-back local changes; after a failure partway, sync stays on the sync branch.

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
  repo with a first commit, records `[sync] branch` (in a new repo the branch `HEAD` is on; in an existing one `main`
  first, then `HEAD`'s branch, then a menu), and checks for a git editor.

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
  from the invocation directory with `GIT_P4SON_REPO_ROOT_DIR` and `GIT_P4SON_BACKEND` set, without stopping at a
  failure. On Windows, scripts run through extension associations, overridable with
  `[hooks.extension-associations]`. `sync` runs the `pre-sync` and `post-sync` hooks.

- **`complete.py`** - Shell completion: `run_complete` walks the argparse parser to suggest commands, flags, actions,
  aliases, split users and the `branch` keyword. Logging is silenced so stdout carries only candidates; the scripts in
  `completions/` call `git p4son complete`.

- **`jj.py`** - Colocated jj workspaces. `operation` pins a jj operation and holds jj's working-copy lock while a
  command runs, yielding a `Workspace`, or None outside jj and with `lock_only`, which the git backend uses and
  which needs no jj. The `Workspace` resolves the change git-p4son works on (the working change, or its parent
  when the working change is empty and new), its bookmark and the default base; commands reach it only through
  `JjBackend`. git still reads objects, diffs and makes the sync commits, and `Workspace.checkout` and
  `restore_working` move the files on disk for sync under the lock; `rebase_branch`, `conflicted_since`,
  `working_is_empty` and `return_to` run jj on the live working copy, only after it. `operation` warns when git's
  `core.autocrlf` (`git.get_autocrlf`) and jj's `working-copy.eol-conversion` disagree.

- **`jj_lock.py`** - Takes jj's local working-copy lock the way jj does, with `flock` on POSIX and `LockFileEx` on
  Windows. A busy lock raises `CommandError` instead of waiting. Hooks must not run jj commands that snapshot, and
  a git-p4son run under its caller's lock needs `--backend git`.

### Shared modules

- **`backend.py`** - The backend: what a command asks of the tool holding the change, as the `Backend`
  interface: the commit to read the change from (`revision`), the base (`resolve_base`, None for the default),
  the alias the branch or bookmark suggests, the default message, the clean check, `-b` completions, `checkouts`
  for the per-commit loop, sync's clean check and commits (`prepare_sync`, `check_sync_clean`, `start_sync`,
  `commit_sync`), and the `Words` its messages use, whose `tool` hooks get as `GIT_P4SON_BACKEND`.
  `selected_backend` picks git or jj from `--backend`, then `core.backend`, where `auto` means jj in a jj workspace.
  `open_backend` holds jj's lock and operation for the whole command and yields the backend in `run_command` as
  `args.backend`; jj snapshots the working copy first unless the command's parser sets `snapshot=False`. Once
  the lock is released, it calls the backend's `after_lock`.
  `completion_backend` gives one without the lock, and `workspace_lock` is `init`'s lock-only path. No command
  branches on jj outside the backend modules and `jj.py`.

- **`git_backend.py`** - `GitBackend`, the git adapter: `HEAD`, `HEAD~1` as the default base, the current branch,
  `checkouts` detaching at each commit and returning to the starting branch, and the sync branch: `--branch`, else
  `[sync] branch`, else `main`, checked out for the sync and returned from with a `git rebase` hint.

- **`jj_backend.py`** - `JjBackend`, the jj adapter, delegating to a `jj.Workspace`. Its sync checks out the sync
  bookmark, commits with jj's line endings and moves the bookmark; `after_lock`, which `open_backend` calls once
  jj's lock is released, runs `jj rebase -b <start> -d <bookmark>` (unless `--no-rebase`), lists conflicts, and
  returns with `jj edit` or `jj new` unless merged-back changes are left. Its `checkouts` raises, as
  `open_backend` refuses the per-commit commands on jj.

- **`lib.py`** - Combines git and Perforce operations: `check_git_workspace_clean`, `create_changelist`,
  `update_changelist`, `include_changes_in_changelist` (returns how many files it opened or moved),
  `revert_unchanged_files`, and `edit_and_shelve`, which `new` and `update` share. They read the change from the
  backend's `revision` and keep the working files around Perforce reverts. On a dry run of `new` the changelist is
  `DRY_RUN_CHANGELIST`.

- **`description.py`** - Models a changelist description (`Description`): the message, the enumerated commit list
  under `Changes included:`, trailing text, and Swarm `#review` keywords on lines of their own. `parse` and `format`
  own the layout; `new` and `update` build descriptions through it.

- **`changelist_store.py`** - Changelist aliases stored in `.git-p4son/aliases/<name>.toml`: save, load, list and
  delete, name validation, the reserved keywords (`last-synced`, `head`, `branch`) and `branch_alias` for the branch
  keyword. The old `.git-p4son/changelists/<name>` store is still read; writes move an alias out of it, and
  `migrate_changelist_aliases`, which `alias clean` runs first, moves the rest.

- **`sync_branch.py`** - The sync branch: `[sync] branch` (`get_sync_branch_setting`), `DEFAULT_SYNC_BRANCH`, and
  `choose_sync_branch`, the `--branch`, setting, `main` order both backends and `init` share.

- **`depot.py`** - Owns the depot root: reading it from config, expanding the `$(workspace)` placeholder, and
  `resolve_depot_root`, which returns it with the client spec (`ResolvedDepot`) for `sync`. `check_depot_root` and
  `subdirectory_depot_root` back `init`'s choice.

- **`config.py`** - Per-repo configuration in `.git-p4son/config.toml`. `get_setting` reads a typed value with a
  default and raises `ConfigError` for a wrong type or invalid TOML; `save_config` merges into the file. Holds the
  depot root, `core.writable`, the split users, the blocking processes and the hook associations.

- **`toml_writer.py`** - `format_toml`, the minimal TOML writer behind `config.toml` and the alias files; reading
  uses `tomllib`.

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
  `normalize_workspace_path`, and the interactive prompts `prompt_choice`, `prompt_menu` and `prompt_numbered`.

- **`log.py`** - All user-facing output goes through the `log` singleton: headings, success, warning and error lines,
  file changes, command echo with a spinner, child process output, and quiet and verbose modes, with
  `log.quiet()` scoping quiet mode to a block. `plural` formats counts.

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
- `git_p4son/jj.py` (`_validate_version`)
- `.github/workflows/test.yml` (`JJ_FLOOR`)
- `README.md` (the Jujutsu section)

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
