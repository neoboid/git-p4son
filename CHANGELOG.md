# Changelog

## Unreleased

- `sync` accepts an explicit `head` keyword (`git p4son sync head`), equivalent to omitting the
  changelist argument

## 0.3.0

- `new`/`update`/`review` no longer abort when p4 refuses to open a file, e.g. git-tracked files matching
  `.p4ignore` ("ignored file can't be added"); the file is skipped with a warning showing p4's reason
- Remove the clobber workspace requirement from `init`
- Sync command now handles writable files automatically without `--force`:
  - Unchanged files (read-only flag removed by git) are detected via git blob comparison and synced normally
  - Changed files (local edits not submitted to Perforce) are three-way merged after syncing
  - Files added both locally and in Perforce are merged against an empty baseline, surfacing both versions
  - Binary files with local changes have their local version restored to disk
  - Git-ignored files are skipped with a warning
  - Add/delete asymmetry between git and Perforce is detected and reported
- The `--force` flag on `sync` now only controls syncing to older changelists
- `sync` warns interactively when the workspace still has the clobber option enabled (no longer needed),
  offering to continue or abort. Continuing dismisses the warning permanently, stored per-user in a
  gitignored `.git-p4son/state.toml`; the prompt is skipped in non-interactive runs

## 0.2.11

- `alias clean` now lists all aliases first and asks whether to delete all of them, review each one
  interactively (the previous flow), or quit
- Fix commands occasionally losing the tail of subprocess output, e.g. sync missing writable-file errors
  from `p4 sync` or opened files from `p4 fstat`
- Fix shell completion offering command echo and spinner control characters as completion candidates
- Fix `new --dry-run` crashing when opening files for edit or combined with `--review`/`--shelve`; dry run
  no longer queries the server for the review keyword and now reports alias problems
- Decode subprocess output as UTF-8 on all platforms; Windows previously used the ANSI code page, garbling
  non-ASCII filenames and commit subjects
- Fix `new`/`update` mangling non-ASCII filenames into quoted escape sequences before passing them to p4
- Alias names are now validated on lookup and delete, not only on save; `alias delete ../../somefile` could
  previously delete a file outside the alias store
- Fix workspace detection walking past git worktree and submodule roots, where `.git` is a file rather than
  a directory
- Re-running `init` no longer deletes unrelated sections (e.g. `[hooks]`) from config.toml, and config
  values containing quotes or backslashes are now written as valid TOML
- `update` no longer duplicates the enumerated commit list: commits in the base branch range replace their
  existing entries (matched by subject), entries outside the range are kept, and the list is renumbered
- `new`/`update` now warn when p4 declines to open a file (e.g. "can't add existing file" or "file(s) not
  in client view") instead of silently leaving it out of the changelist
- `review` no longer fails the rebase when the branch contains merge commits (they are skipped; the merged
  commits themselves are still included), and a multiline `-m` message is rejected up front instead of
  producing a broken rebase todo
- Re-shelving now clears the previous shelf first, so files no longer open (e.g. added in one commit and
  deleted in a later one) do not linger in the review as stale shelved entries
- `new`/`update` no longer abort on typechanges (treated as edits) or copies (treated as adds of the
  destination) in the git diff
- Fix branch detection during an interactive rebase when the command is run from a subdirectory of the
  workspace
- A failing post-sync hook no longer prevents the remaining hooks from running; hook stderr is now printed,
  and the default Windows association for `.nu` hooks points at `nu.exe` (Nushell's actual binary)
- Re-running `init` now recovers a repo whose initial commit failed (e.g. missing `user.email`) instead of
  treating it as already initialized; Ctrl-D at the depot root prompt aborts cleanly
- Redirected output (e.g. `git p4son sync > log.txt`) no longer contains spinner frames, carriage returns,
  and ANSI escape sequences; the spinner only runs on an interactive terminal
- An invalid `--sleep` value is now rejected by argument parsing instead of erroring after the command
  already did its work
- Alias validation now rejects all-digit names (they always parse as changelist numbers, so such an alias
  could never be referenced) and Windows reserved device names (`CON`, `NUL`, `COM1`, ...)
- A `log.decorate` git config no longer leaks branch decorations into changelist descriptions and review
  rebase todos
- `update` no longer mistakes a numbered list inside the user's own message for the commit list; the list
  is anchored on the "Changes included:" heading
- The spinner no longer garbles the error message when an executable (e.g. `p4`) is not on PATH

## 0.2.10

- Add support for git style post-sync hooks
Could be used with [git-p4son-chmod](https://github.com/fu5ha/git-p4son-chmod) to remove read-only flag set by p4
of git managed files.
- Allow p4 opened files if they're not tracked by git repository during sync

## 0.2.9

- Set PWD env var for subprocesses to match the requested cwd
- Validate alias names to ensure they are usable as filenames
- Remove p4 sync -n dry run to speed up sync

## 0.2.8

- Fix bug when a commit first added a file and then edited it in a later commit when creating a review

## 0.2.7

- Fix files not reopened when p4 action mismatches git status
For instance, if a file was opened for edit in perforce and a commit deletes the file.

## 0.2.6

- Rename `alias set` command to `alias new`
- Clean up output of alias commands

## 0.2.5

- Use -ztag style perforce commands
- Polish output of commands
- Add --no-desc option to `update` command to skip updating changelist description
- Add a expanded section to `README.md` explaining why clobber is needed

## 0.2.4

- Scope p4 opened check to depot root in sync command
- Improve docs on depot root

## 0.2.3

- Enable syncing just a sub-directory of your perforce workspace
- Bump minimum Python to 3.11 to gain access to tomllib

## 0.2.2

- Improve output for editor validation on init
- Add git editor setup step to README

## 0.2.1

- Verify that git editor is set in init subcommand
- Validate that editor can be resolved before running sequence editor in review subcommand
- Improve setup section suggesting that the first sync needs to committed manually
- Add bash shell completion

## 0.2.0

- Link to shell completions from install section
- Update readme with section on big workspaces
- Remove 'latest' from sync suggestion
- Fix some format log strings
- Include workspace name in init clobber error message

## 0.1.9

- Add CHANGELOG with release notes since 0.1.0

## 0.1.8

- Make -m/--message optional for new and review commands
- Improve output of all commands
- Ensure that alias do not exist even in dry-run mode for review command
- Show label of changelist in heading when syncing

## 0.1.7

- Use magenta color for headings
- Improve output of p4_sync command
- Add success, warning and consolidate output with pre-existing error
- Log opened files before error message on clean check

## 0.1.5

- Use orange for untracked file prefix
- Show dirty file list when workspace is not clean
- Fix files not ending up in the correct changelist
- Add upgrade instructions to README
- Resolve branch name during interactive rebase detached HEAD

## 0.1.4

- Update README and help text for new defaults
- Default alias option to current branch, add --no-alias flag
- Make sync changelist arg optional, remove `latest` keyword
- Update Setup section to foreground the init subcommand

## 0.1.3

- Add init command to set up git repo inside a Perforce workspace
- Fail with clear error when using last-synced without prior sync
- Rewrite first-time setup instructions in README
- Document all commands, flags, and branch keyword in README

## 0.1.1

- Update README with pip install and completion subcommand instructions

## 0.1.0

- Initial release
