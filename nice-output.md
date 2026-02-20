# Brief

I would like to consolidate and clean up what git-p4son prints to STDOUT/STDERR.

What I am after is that the user should see what the tool is up to, a
short line describing each step before it tries to do things.
then once it is done I want to see the result it was after.

For instance, say that a user has executed the following command:
```
git p4son review new branch -m "message"
```

and that their local git branch is currenly called `feat/part1`.

Then after the first parts of `run_command()` has executed I want the user to see something like this.

```
# Finding workspace directory
root: /Users/TheUser/project/

# Resolving alias from current git branch
> git rev-parse --abbrev-ref HEAD
git branch: feat/part1
alias: feat-part1
```

... and so on.

I may want to color code the output later, or change how the different types of
lines are formatted. Maybe I want the heading-lines to be prefixed with something
else than # as we dive deeper into this nice-output feature.

We may also have to play with different verbosity levels configurable from the commandline.


Lets analyze and suggest how to go about structuring output this way.

`git p4son sync latest` has a lot of small substeps, and a lot of output. Let's use that
as a exploration test-bed and try to come up with a good design. Concentrate on designing
output for now, and not so much about concrete implementation.

Please ask me interactive questions if you need to get a better idea of what I want.

## note 1

I like it so far, let's also think about what happens if a subprocess fails. We don't need to print as
much context at that point as we already have the headings, a plain "Failed with return code X" should be enough.

## Note on long running commands
Long running commands like "p4 sync" may take a long time, from seconds to tens of minutes.
It is hard to do a process bar because "p4 sync" seem to print each file as the command starts, and not when
each file is done syncing.
Unless there is a way to tell p4 sync to give us this info then we should at least show an ascii-spinner
while it is churning along.

## Lets make an implementation plan

The plan looks solid enough now. Lets make a detailed implementation plan, with appropriately sized steps.
We should commit each step as a separate CL for easier review.
All output should be structured after we are done no roughe `print` calls outside the log module.

Place the plan in the section at the bottom, leaving the rest for reference


### Additional requirements
Make sure to format all code after each step, before committing.
Make sure all test pass before committing and moving on to next step

## First impressions after testing the implemntation

It looks good!

Now I want to add colors.
Heading line should be yellow
the `>` should be turquise
The error line should be prepended with "Error:" that is red, the rest of the line can be uncolored/white, where we detail return code etc
raw stderr output should be uncolored/white
all other lines should be uncolored/white


# Color plan

## Color scheme

| Line type | Color |
|-----------|-------|
| Heading (`# ...`) | Yellow (entire line) |
| Command prompt (`>`) | Cyan/turquoise (just the `>`, not the command text) |
| `error()` output | Red `Error:` prefix, rest uncolored |
| `fail()` output | Red `Failed` prefix, rest uncolored |
| Everything else (detail, info, verbose, stdin, elapsed, raw stderr) | Uncolored/white |

## TTY detection

Colors auto-detect: on when stdout/stderr is a TTY (terminal), off when piped to a file or another command.
Uses `stream.isatty()` from the standard library.

## Changes to `git_p4son/log.py`

Add ANSI color constants and a TTY-aware helper at module level:

```python
_YELLOW = '\033[33m'
_CYAN = '\033[36m'
_RED = '\033[31m'
_RESET = '\033[0m'

def _use_color(stream) -> bool:
    return hasattr(stream, 'isatty') and stream.isatty()
```

Add semantic aliases so methods reference purpose, not raw color:

```python
_HEADING_COLOR = _YELLOW
_COMMAND_COLOR = _CYAN
_ERROR_COLOR = _RED
```

Add a `_color(text, color, stream)` helper that wraps text in ANSI codes only when the stream is a TTY.

Methods to update:
- `heading()` — wrap entire line in yellow
- `command()` — wrap just `>` in cyan. The `_spinner_line` stores **plain** text (no ANSI codes) so
  `\r` reprinting works correctly. Only the initial `print()` gets color.
- `stop_spinner()` — when reprinting the clean line, apply cyan to `>` to match the original output
- `error()` — prepend with red `Error: ` prefix
- `fail()` — wrap `Failed` in red, rest uncolored

## Changes to `tests/test_log.py`

Existing tests use `capsys` which is not a TTY, so `_use_color()` returns False — all existing
assertions stay unchanged (no ANSI codes in output).

Add a `TestColor` class that patches `isatty` to return True:
- Heading contains yellow ANSI codes
- Command `>` contains cyan ANSI codes
- Error output contains red `Error:` prefix
- Fail output contains red `Failed` prefix

## Files to modify
- `git_p4son/log.py`
- `tests/test_log.py`

## Verification
- `python -m pytest tests/` — all tests pass
- `autopep8 -i -r git_p4son/ tests/`
- Manual: run a command in a terminal to visually verify colors


# Analysis and plan

## Current output audit

Every call to `run()` and `run_with_output()` unconditionally prints two lines:
```
> p4 info
Elapsed time is 0:00:00.150
```

`P4SyncOutputProcessor` adds per-file lines during `p4 sync`:
```
upd: path/to/file.cpp
     progress: 3 / 12
     sync stats file count 3, time 0:00:01.234
```

Command modules (`sync.py`, `new.py`, etc.) print ad-hoc status messages via bare `print()` calls.
Errors go to `sys.stderr` via `print(..., file=sys.stderr)`.

The result is a flat, noisy stream where important info (like "which changelist?") is buried among
timing noise and repeated for every subprocess call.

## Design decisions

Based on discussion:

| Concern | Decision |
|---------|----------|
| `> command` lines | Always shown (all verbosity levels) |
| Elapsed time | Only for long-running commands (p4 sync). Normal = p4 sync only. Verbose = all commands. |
| Per-file sync progress | Verbose only. Normal shows just the summary. |
| Printer architecture | Module-level global singleton. Safe because all output callbacks run on the main thread (threading in `run_with_output` is only for non-blocking IO). |
| Spinner | Show an ASCII spinner at the end of the `> command` line for all commands, overwritten in-place. Harmless for fast commands, useful for slow ones. Progress bar not feasible since p4 reports files at start of sync, not completion. |
| Error handling | Minimal. Heading + command already provide context. Just print "Failed with return code X". |
| Color support | Design for it (formatter methods), implement later. |

NOTE: The spinner is placed at the end of the `> command` line, overwritten in-place while the
command runs. When the command finishes, the spinner is cleared (line is reprinted without it).
This applies to all commands — it's harmless for fast ones and helpful for slow ones.

## Output line types

All output goes through a central log object with typed methods. This makes it easy to
later change formatting, add colors, or filter by verbosity.

| Method | Example | Verbosity |
|--------|---------|-----------|
| `heading(text)` | `# Checking git workspace` | always |
| `command(cmd)` | `> p4 info` | always |
| `detail(key, value)` | `last synced: CL 54320` | always |
| `info(text)` | `clean` or `12 files to sync` | always |
| `verbose(text)` | `upd: path/to/file.cpp` | verbose only |
| `stdin(text)` | `stdin:\n  Change: new\n  ...` | verbose only |
| `elapsed(duration)` | `elapsed: 0:00:05.123` | see table above |
| `error(text)` | `Error: workspace is not clean` | always (stderr) |
| `fail(returncode)` | `Failed with return code 1` | always (stderr) |

Blank lines between sections are emitted by `heading()` (prints a blank line before the heading,
except for the first one).

## Error handling

Currently `main()` in `cli.py` catches `RunError` and dumps all stderr lines plus
"Command failed with exit code X". With structured output, the heading tells you what step failed
and the `> command` line tells you what command failed. So error output can be minimal:

```
# Syncing to CL 54321
> p4 sync //...@54321
Failed with return code 1
```

The heading (`# Syncing to CL 54321`) and command (`> p4 sync ...`) already provide all the context.
No need to repeat them or print additional framing.

**What about stderr from the subprocess?** p4 commands sometimes put useful error details in stderr
(e.g., "file(s) not on client"). Proposal: at normal verbosity, show just the first line of stderr
(if any) plus the return code. At verbose, show all stderr lines. This keeps normal output clean
while making debugging possible with `-v`.

```
# normal
# Syncing to CL 54321
> p4 sync //...@54321
//depot/... - file(s) not on client.
Failed with return code 1

# verbose — same, but with all stderr lines if there are multiple
```

**Where this is handled:** The top-level `except RunError` in `main()` currently does the printing.
With the new log object, it would call `log.error()` for the stderr line and
`log.fail(returncode)` for the return code line. The `log.fail()` method is a new line type.

**`CommandError` (logic/validation errors):** These are raised for things like "No client name found
in p4 info output". These already have a clear message, so just `log.error(str(e))` is enough.

NOTE: Error messages must provide specific detail about *what went wrong*, not just restate the
heading. E.g., "No client name found in p4 info output" is good — it tells you something new.
"Failed to find latest changelist" would be bad — it just echoes `# Finding latest changelist`.

## Changes to `run()` / `run_with_output()`

Currently these functions own the `> command` and `Elapsed time` printing. With the new design:

- They call `log.command()` to print the `> command` line (always).
- They **stop** printing elapsed time. Instead, they make timing info available
  (e.g., return it as part of `RunResult`, or the caller measures it externally).
- The caller decides whether to show elapsed time via `log.elapsed()`.

## Proposed output: `git p4son sync latest`

### Normal verbosity (default)

```
# Checking git workspace
> git status --porcelain
clean

# Checking p4 workspace
> p4 opened
clean

# Finding last synced changelist
> git log -1 --pretty=%s --grep=: p4 sync //\.\.\.@
last synced: CL 54320

# Finding latest changelist
> p4 info
> p4 changes -m1 -s submitted //my-client-name/...#head
latest: CL 54321

# Syncing to CL 54320
> p4 sync -n //...@54320
all files up to date

# Syncing to CL 54321
> p4 sync -n //...@54321
12 files to sync
> p4 sync //...@54321 /   <-- spinner at end of command line, overwritten in-place
synced 12 files (add: 3, upd: 8, del: 1)
elapsed: 0:00:05.123

# Committing git changes
> git add .
> git commit -m "git-p4son: p4 sync //...@54321"

Done
```

### Verbose (`-v`)

Same as normal, plus:
- Elapsed time shown for ALL commands (not just p4 sync)
- Per-file progress during p4 sync:

```
# Syncing to CL 54321
> p4 sync -n //...@54321
12 files to sync
> p4 sync //...@54321
upd: path/to/file1.cpp          (1/12)
upd: path/to/file2.h            (2/12)
add: path/to/newfile.cpp         (3/12)
del: path/to/removed.cpp         (4/12)
...
synced 12 files (add: 3, upd: 8, del: 1)
elapsed: 0:00:05.123
```

NOTE: In verbose mode, also print the raw stdout of each command. Exception: `p4 sync` already
reformats its output (per-file progress above), so it does not additionally dump raw stdout.

Example — verbose `git commit` output:
```
# Committing git changes
> git commit -m "git-p4son: p4 sync //...@54321"
[main abc1234] git-p4son: p4 sync //...@54321
 12 files changed, 150 insertions(+), 30 deletions(-)
elapsed: 0:00:00.045
```

At normal verbosity, the same section would just be:
```
# Committing git changes
> git add .
> git commit -m "git-p4son: p4 sync //...@54321"
```

### Edge cases

When sync target equals last synced:
```
# Finding last synced changelist
> git log -1 --pretty=%s --grep=: p4 sync //\.\.\.@
last synced: CL 54321

# Finding latest changelist
> p4 info
> p4 changes -m1 -s submitted //my-client-name/...#head
latest: CL 54321

Already at CL 54321, nothing to do.
```

When syncing to an older changelist without `--force`:
```
...
Cannot sync to CL 54300 (currently at CL 54321) without --force.
```

When a subprocess fails (e.g., p4 is unreachable):
```
# Checking p4 workspace
> p4 opened
Connect to server failed; check $P4PORT.
Failed with return code 1
```

When git workspace is dirty (validation error, not a subprocess failure):
```
# Checking git workspace
> git status --porcelain
workspace is not clean, aborting
```

## Sketch for other commands

### `git p4son new -m "Fix bug" myalias -b main --review`

```
# Finding workspace directory
root: /Users/me/project

# Creating changelist
> p4 change -i
created: CL 67890
alias: myalias -> 67890

# Opening files for edit
> git merge-base main HEAD
> git diff --name-status abc123..HEAD
3 files changed (2 modified, 1 added)
> p4 edit -c 67890 path/to/modified1.cpp
> p4 edit -c 67890 path/to/modified2.cpp
> p4 add -c 67890 path/to/new_file.cpp

# Adding review keyword
> p4 change -o 67890
> p4 change -i

# Shelving
> p4 shelve -f -Af -c 67890

Done
```

NOTE: In verbose mode, show the stdin input passed to commands like `p4 change -i`. This helps
debug what spec content is being submitted. Example:

```
# Creating changelist
> p4 change -i
stdin:
  Change: new

  Description:
  	Fix the login bug

  	Changes included:
  	1. Fix null check in auth handler
created: CL 67890
```

This applies to any command that receives input via stdin (currently `run()` with `input=...`).

### `git p4son update myalias --shelve`

```
# Finding workspace directory
root: /Users/me/project

# Resolving alias
myalias: CL 67890

# Updating changelist description
> p4 change -o 67890
> git log --oneline --reverse main..HEAD
> p4 change -i

# Opening files for edit
> git merge-base main HEAD
> git diff --name-status abc123..HEAD
3 files changed (2 modified, 1 added)
> p4 edit -c 67890 path/to/modified1.cpp
> p4 edit -c 67890 path/to/modified2.cpp
> p4 add -c 67890 path/to/new_file.cpp

# Shelving
> p4 shelve -f -Af -c 67890

Done
```

## Architecture sketch

A new module `git_p4son/log.py` with:

```
log = Log()                # module-level singleton, imported everywhere

log.heading("...")         # section heading
log.command("...")         # subprocess command
log.detail("key", val)    # key-value result
log.info("...")            # status text
log.verbose("...")         # verbose-only text
log.stdin(text)            # stdin input passed to a command (verbose only)
log.elapsed(duration)      # timing (respects verbosity rules)
log.error("...")           # stderr
log.fail(returncode)       # "Failed with return code X"
```

Configured once in `main()` based on `--verbose` flag (added to the top-level parser).

`run()` and `run_with_output()` call `log.command()` instead of their current `print('>', ...)`
and stop printing elapsed time. Callers that want elapsed time measure it themselves and call
`log.elapsed()`.

## Resolved decisions

- **Heading prefix**: Not configurable from CLI, but hardcoded in the log module so it's easy to
  change later (single constant).
- **Spinner**: Simple ASCII spinner (`|/-\`) for now. Can revisit with fancier Unicode spinners later.

## Open questions for later

- Color scheme: which line types get which colors?
- Should `--quiet` suppress headings and only show key results + errors?


# Detailed implementation plan

91 `print()` calls across 11 source files, 97 output assertions across 8 test files.

Each step below is one commit. Tests are updated alongside the code they test.

## Step 1: Create `git_p4son/log.py`

Create the `Log` class and module-level `log` singleton. Pure addition — no existing code changes.

**New file: `git_p4son/log.py`**
- `Log` class with `verbose_mode` flag (default `False`)
- Methods: `heading()`, `command()`, `detail()`, `info()`, `verbose()`, `stdin()`, `elapsed()`,
  `error()`, `fail()`
- Heading prefix as a module constant (easy to change later)
- `heading()` emits a blank line before the heading (except the first call)
- `verbose()` and `stdin()` are no-ops when `verbose_mode` is False
- `error()` and `fail()` write to stderr
- Module-level singleton: `log = Log()`

**New file: `tests/test_log.py`**
- Test each method's output format
- Test verbose vs non-verbose filtering
- Test heading blank-line logic

**Files changed:** 2 new files

## Step 2: Add `--verbose` / `-v` flag to CLI

**`cli.py`**
- Add `-v` / `--verbose` to the top-level parser (before subparsers)
- In `main()`, set `log.verbose_mode = args.verbose` before dispatching to `run_command()`

**`tests/test_cli.py`**
- Test that `--verbose` flag is parsed

**Files changed:** `cli.py`, `tests/test_cli.py`

## Step 3: Convert `common.py`

Replace the print statements in `run()` and `run_with_output()` with log calls. This is the
highest-impact single change since every command uses these functions.

**`common.py`**
- `import` the `log` singleton from `log.py`
- `run()`: replace `print('>', ...)` with `log.command()`; remove `print('Elapsed time is', ...)`
- `run_with_output()`: same replacements
- `run()`: add `log.stdin(input)` when `input` is provided
- Add `elapsed` field to `RunResult` (timedelta)
- Both functions populate `result.elapsed` from their timing code
- Keep the CTRL-C messages as `log.error()` calls

**`tests/test_common.py`**
- Update output assertions (no more "Elapsed time is ..." in captured output)
- Test that `RunResult.elapsed` is populated
- Update `helpers.py`: add `elapsed` parameter to `make_run_result()`

**Files changed:** `common.py`, `tests/test_common.py`, `tests/helpers.py`

## Step 4: Convert `sync.py`

The biggest module (33 print calls). Add section headings and replace all print calls.

**`sync.py`**
- Import `log`
- `sync_command()`: add headings for each section:
  - `# Checking git workspace`
  - `# Checking p4 workspace`
  - `# Finding last synced changelist`
  - `# Finding latest changelist` (or `# Resolving changelist`)
  - `# Syncing to CL <N>` (once or twice depending on catch-up)
  - `# Committing git changes`
- Replace `print('clean')` / `print('dirty')` with `log.info()`
- Replace `print(f'Latest changelist ...')` with `log.detail()`
- `P4SyncOutputProcessor`: per-file lines become `log.verbose()`, summary becomes `log.info()`
- `p4_sync()`: show `log.elapsed()` for the sync command (use `RunResult.elapsed`)
- Remove `echo_output_to_stream` — replaced by `log.verbose()` in the callback
- Remove `green_text()` (color support deferred to later)
- Remove bare `print('')` separators (heading() handles spacing)

**`tests/test_sync.py`**
- Update output assertions for new heading/detail format
- Remove assertions on "Elapsed time is ..."

**Files changed:** `sync.py`, `tests/test_sync.py`

## Step 5: Convert `cli.py`

**`cli.py`**
- Import `log`
- `run_command()`: add `log.heading("Finding workspace directory")` +
  `log.detail("root", workspace_dir)`
- `_resolve_branch_keyword()`: add `log.heading("Resolving alias from current git branch")` +
  `log.detail()` calls for branch and alias
- `main()` error handling:
  - `except RunError`: `log.error()` for first stderr line, `log.fail(e.returncode)`
  - `except CommandError`: `log.error(str(e))`
  - `except Exception`: `log.error(str(e))`
  - `except KeyboardInterrupt`: `log.error("Operation cancelled by user")`
- Replace remaining `print(..., file=sys.stderr)` calls with `log.error()`

**`tests/test_cli.py`**
- Update output assertions

**Files changed:** `cli.py`, `tests/test_cli.py`

## Step 6: Convert `lib.py`

**`lib.py`**
- Import `log`
- `create_changelist()`: dry-run messages → `log.info()`
- `update_changelist()`: dry-run messages → `log.info()`
- `add_review_keyword_to_changelist()`: status/dry-run messages → `log.info()`

**`tests/test_lib_changelist.py`, `tests/test_lib_edit.py`, `tests/test_lib_review.py`**
- Update output assertions

**Files changed:** `lib.py`, `tests/test_lib_changelist.py`, `tests/test_lib_edit.py`,
`tests/test_lib_review.py`

## Step 7: Convert `new.py` and `update.py`

Small modules (3 + 1 print calls). Combined into one commit.

**`new.py`**
- Import `log`
- Add headings: `# Creating changelist`, `# Opening files for edit`,
  `# Adding review keyword`, `# Shelving`
- Replace `print(f"Created changelist ...")` with `log.detail("created", f"CL {changelist}")`
- Replace alias saved message with `log.detail()`
- Replace alias exists error with `log.error()`

**`update.py`**
- Import `log`
- Add headings: `# Resolving alias`, `# Updating changelist description`,
  `# Opening files for edit`, `# Shelving`
- Replace `print(f"Updated changelist ...")` with `log.detail()`

**Tests**
- Update output assertions in any tests covering new/update commands

**Files changed:** `new.py`, `update.py`, and their tests (if any)

## Step 8: Convert `review.py`

**`review.py`**
- Import `log`
- `review_command()`: add headings, replace prints
- `sequence_editor_command()`: replace error prints with `log.error()`
- Dry-run output → `log.info()`

**`tests/test_review.py`**
- Update output assertions

**Files changed:** `review.py`, `tests/test_review.py`

## Step 9: Convert `alias.py`, `changelist_store.py`, `list_changes.py`

**`alias.py`**
- Import `log`
- `alias_list_command()`: output via `log.info()`
- `alias_set_command()`: `log.detail()` for saved alias, `log.error()` for invalid CL
- `alias_delete_command()`: `log.info()` for deletion confirmation
- `alias_clean_command()`: **special case** — interactive prompts use `print()` and `input()`.
  The listing and deletion confirmations use `log.info()`, but the `input()` prompt stays
  as-is since it's an interactive UI, not structured output.
- `alias_command()`: error message → `log.error()`

**`changelist_store.py`**
- Import `log`
- Replace `print(..., file=sys.stderr)` with `log.error()`

**`list_changes.py`**
- Import `log`
- Replace `print(description)` with `log.info()`
- Replace `print("No changes found")` with `log.info()`

**Files changed:** `alias.py`, `changelist_store.py`, `list_changes.py`,
`tests/test_list_changes.py`

## Step 10: Add spinner

Implement the ASCII spinner in the log module and integrate with `run()` / `run_with_output()`.

**`log.py`**
- Add `start_spinner()` and `stop_spinner()` methods
- Spinner uses `|/-\` characters, overwrites end of current line with `\r`
- `stop_spinner()` clears the spinner character and reprints the clean line
- Spinner runs via a background thread that updates every ~100ms

**`common.py`**
- `run()`: call `log.start_spinner()` before subprocess, `log.stop_spinner()` after
- `run_with_output()`: call `log.start_spinner()` before subprocess,
  `log.stop_spinner()` after. In verbose mode, the spinner is suppressed since per-line
  output is being printed instead.

**`tests/test_log.py`**
- Test spinner start/stop (mock time/threading)

**Files changed:** `log.py`, `common.py`, `tests/test_log.py`

## Step 11: Final audit

- Grep for remaining `print()` calls outside `log.py`
- Verify only legitimate exceptions remain:
  - `complete.py` — tab completion output consumed by shell (must stay as `print()`)
  - `alias_clean_command()` — interactive prompt responses
- Run full test suite
- Manual smoke test of `git p4son sync latest` output

**Files changed:** possibly minor fixups

## Exceptions (print calls that intentionally stay)

- **`complete.py`**: prints tab completion candidates for the shell. Not user-facing output.
- **`alias_clean_command()`**: interactive `input()` / `print()` loop. The prompt-response
  cycle doesn't fit the heading/detail model.
