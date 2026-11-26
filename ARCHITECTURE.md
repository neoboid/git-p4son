# Architecture

The module-by-module map of `git_p4son/`. `CLAUDE.md` covers `cli.py` and the rules every change follows.

## Commands

- **`sync.py`** - The `sync` command: syncs the git repo with Perforce, one git commit per changelist, onto the sync
  branch from any branch. `_plan_sync` resolves the arguments (one or more strictly increasing CL numbers or sync
  targets, `last-synced`, or a trailing `head`; none means `[sync] default-target`, else the latest) into a `_SyncPlan`,
  reading the last sync from the sync branch the backend's `prepare_sync` chose, or None when already synced.
  `sync_command` hands the split queries to `sync_passes.sync_on_branch` as `before_run`, so they run after its checks
  and the pre-sync hooks, and fail before any sync. It then runs a catch-up pass to the last synced CL and syncs and
  commits each target (`sync_passes.run_sync_passes`), on the sync branch (`sync_passes.sync_on_branch`). With sync
  progress on disk, it resumes the interrupted sync instead of planning (`sync_resume.resume_interrupted_sync`).
  Changelist arguments are never aliases: aliases name pending CLs, which Perforce renumbers on submit. When moving
  forward, `build_sync_targets` also syncs the CL before each split user's submit, so each of their changelists gets a
  commit of its own; `_split_targets` finds their CLs per user, then checks them all against the depot root in one
  batched `-m2` query. `-u` adds users for one run and `--no-split` ignores the configured ones.

- **`sync_passes.py`** - A sync's passes and its end, which `sync`, its resume and `cherry-pick` share. `sync_on_branch`
  runs a sync on the sync branch, given an `OnSyncBranch` (the parsers add its options with `_add_sync_branch_args`):
  `sync_preflight` refuses to start while a blocking process runs (unless `--ignore-blocking-processes`), or while
  either workspace is dirty, the Perforce one judged by the sync branch's files; then the checkout, the pre-sync hooks,
  the run, and `finish_on_branch`, which runs the post-sync hooks, where a Ctrl-C or an error stops the hooks and not
  the return (`_run_post_sync_hooks`), with exit code 1, and passes the sync branch's `SyncState` to `finish_sync` as
  text. `finish_sync` returns to where sync started, unless changes are left in the work tree, as merged-back local
  changes; after a failure partway, sync stays on the sync branch, and says to run `git p4son sync` to finish or
  `--abort` to give up (`fail_sync`, resumable while the sync progress exists). `run_sync_passes` syncs a list of
  catch-up `SyncPass`es (`catch_up_passes`) folded into the first target's commit, then commits each target, through
  `run_passes`; a pass's `revision` is `@N`, or `@=N` with `exact`, which also picks the cherry-pick subject. `end_sync`
  then merges the local changes back once (`sync_merge.py`). `sync_settings` gives the depot root, the line endings and
  the clobber option, and `find_sync_state` the sync branch's `SyncState`. `sync` alone passes `record_progress` to
  `run_sync_passes`, which keeps the sync progress (`sync_progress.py`) through a `ProgressRecorder`: the first pass's
  lists just before its `p4 sync`, a pass dropped once its sync finishes (catch-up) or its commit is made (target,
  moving the recorded tip), each local change dropped once merged back, and the file removed once the end has run,
  before the post-sync hooks. A resume runs the interrupted pass with its stored lists (`_resume_pass`), as classifying
  its files again would read the ones it made read-only as unchanged.

- **`sync_resume.py`** - Resuming an interrupted sync from its sync progress, or giving it up. `resume_interrupted_sync`
  runs when sync progress is on disk, refusing the options that make a plan, a changelist argument, `--branch`, `-u`,
  `--no-split`, `--allow-backwards` and `--force`, even ones that repeat it (`_check_no_plan_options`), and a backend
  other than the one `[start]` names: the backend checks where the interrupted sync left the sync branch
  (`resume_sync`), and `_sort_commits` sorts the commits above the recorded tip (`Backend.commits_above`): a first one
  with the next pass's subject is that pass's own, made just before the interruption could record it (the crash window),
  any other sync or cherry-pick commit makes the sync progress stale and refuses, naming `sync --abort`, and the rest
  are the user's; the blocking-process and p4-opened checks run, and `_sort_files` stands in for the clean check,
  sorting git's changes since the recorded tip against the interrupted pass's stored file list, and the uncommitted
  files of the catch-up passes made before it, and `perforce.p4_off_have` (`p4 diff -se`): a file the pass lists,
  changed in git and matching the have-list, was synced by the interrupted run, and a file merged back before the
  interruption is expected too. A listed tracked file clean in git that differs from the have-list, as after a `git
  reset --hard` or a local change p4 had not reached, is behind it, and so is one p4 deleted that is back, and one p4
  added that is gone again, by a `git clean` or a checkout, which git sees neither changed nor tracked, so it is judged
  for being missing; p4's leftovers (`_p4_leftovers`) are the `tmp.<pid>.<hex>.<n>` files beside the pass's files and
  the pass's adds on disk that a `p4 sync -n` of them still lists as adds, and its deletes, which an abort's pass adds
  back, (`p4_sync_pending`), unless the stored lists account for them as ignored or added on both sides, and only when
  the file holds the start of the depot revision p4 was writing, compared over the first MiB of each, as the file can be
  large (`p4_print_start`), as p4 leaves it, line endings aside, any other being the user's, found from p4, not git, so
  ignored folders too; a symlink or a folder at such an add is refused before anything reads it (`_refuse_links`,
  `_refuse_folders`), as the compare cannot read a link, nor a save keep one, and p4 cannot write the add over a folder.
  Any other changed file is an intersync change, one the user made while the sync was interrupted (`_Intersync`): once
  the hooks have run, `_save_intersync` commits each such file's content under `ours/` (`git.commit_files`, kept alive
  by `refs/git-p4son/intersync-changes`), and records the save; `_put_right` then puts the files back as the sync
  expects them (the have revision in the pass, else the recorded tip's content with `git.restore_files`, gone where
  there is none), and `_complete_saves` adds the have revision's content under `have/`, the base of a file p4 had
  transferred, as no commit holds it; a file p4 had not reached is measured from the recorded tip, which
  `p4_sync_pending` tells. A symlink, a git repository or a folder in place of a file among them is refused, as a save
  holds file content. The pre-sync hooks run again (a failing one leaves it all as it was), then the backend puts the
  sync branch at the recorded tip or the crash window's commit (`resume_at`), before the records: `_unwind` keeps the
  user's commits under `refs/git-p4son/intersync-commits`, joined to an earlier resume's (`git.join_commits`), and
  records them before the move takes the branch back below them, their content then sorted with the work tree and named
  again at the end, and `_take_crash_commit` counts the crash window's commit as the pass made, then `_put_right` writes
  the files behind the have-list again with `p4 sync -f <file>#have` (`perforce.p4_sync_to_have`), removes the deleted
  ones, clears the leftovers, and, under noclobber, makes read-only again a tracked file of the pass made writable since
  without an edit, as the resumed pass doesn't prepare its files again, and keeps a git-ignored one made writable since,
  which git has no copy of to tell an edit by, adding it to the stored ignored files, whose refusal the pass expects,
  and the pass records that it is put right (`_record_put_right`), after which a file p4 has not reached is at its have
  revision, and an edit to it is measured from there, not from the recorded tip, and `_resume_sync_passes` runs the
  interrupted pass with its stored lists (`sync_passes.run_passes`) and the later ones as usual against the pre-sync
  commit, and `sync_passes.end_sync` covers every pass, the passes made before the interruption among them, local
  changes staged again from git (`stage_changed_files`) and the earlier merge outcomes reported with this run's, then
  the intersync changes, with theirs, oldest save first (`_saved_changes`), and the ref is deleted with the sync
  progress. A dry run stops after the checks, saying what is left. `--abort` goes through the same checks, sorting and
  putting right, then `_abort` replaces the passes left in the sync progress with the catch-up to the sync branch's last
  commit (`catch_up_passes`), uncommitted, when the interrupted pass's p4 sync or a catch-up pass's had run, and records
  them before their p4 sync, so the resume that runs them, then or after an interruption, is the abort, which the sync
  progress marks (`aborting`), so a later resume says it finishes the abort; the first runs with the interrupted pass's
  stored lists, as every sync catches up to its last sync first, so going back there moves only that pass's files. An
  abort of stale sync progress (`StaleSyncProgress`) only removes it, naming any changes it saved, which only the
  intersync ref keeps then, and one with no synced changelist to go back to, a first sync, refuses.

- **`cherry_pick.py`** - The `cherry-pick` command, refused while an interrupted sync waits
  (`check_no_interrupted_sync`): syncs only the files of submitted changelists (`p4 sync @=N`) onto the sync branch,
  through `sync_passes`' `sync_on_branch` and `run_sync_passes`. It refuses a changelist at or before the last synced
  one (`find_sync_state`), with no override, and one that isn't submitted (`describe_changelist`), and warns about one
  older than an earlier cherry-pick. Its catch-up passes (`catch_up_passes`, which `sync last-synced` uses too) sync to
  the last synced changelist, then each cherry-pick since it in order, so the workspace matches the sync branch before
  the new ones.

- **`sync_split_users.py`** - The split users: reading and writing the `sync.split-users` list, written in place
  (`set_setting_in_place`) so the rest of `config.toml` keeps its comments and layout, resolving the `$(user)`
  placeholder to the current Perforce user (`resolve_split_users`), and the `sync-split-users` command (`list`, `add`,
  `delete`).

- **`new.py`** - Creates a Perforce changelist from the git change (`create_changelist`) and saves an alias for it (the
  branch name by default; `--no-alias` skips it) with the record `included.py` builds, then opens the changes and
  reverts the files no longer part of them through `edit_and_shelve`. `--review` (or `--review-tool`) resolves the
  review tool first (`resolve_review_tool`) and shelves, recording the tool in the alias, Swarm included; Swarm gets a
  `#review` keyword, and any other tool runs after the shelve (`push_review`), before the record counts the commits.
  `--shelve` only shelves. A failure once the CL exists names the `update` that finishes it. Requires a clean git
  workspace. `--per-commit` instead builds the changelist commit by commit through `run_per_commit`, running `new
  <alias>` on the first commit and `update <alias>` on the others, both shelving with `--shelve` or `--review`, with
  `shelf_delay` between them. Before shelving, `check_at_latest_revision` refuses files of the change that aren't at the
  latest revision, unless `--ignore-out-of-date`; a per-commit run checks every commit once, and its steps
  (`per_commit_step`) skip it.

- **`update.py`** - Updates an existing changelist, named by number or alias, once `check_own_pending` accepts it (once
  per `--per-commit` run, not in its steps). With a record (`find_record`), it folds in the commits after the included
  ones, through `included.plan_update`, appending their subjects; without one, it merges the commits since `-b` into its
  description by subject (`update_changelist`). Then `edit_and_shelve`, after a shelve the review tool an alias records
  for the CL (`find_review`, looked up once and checked before shelving), through `push_review`, and the record's new
  count. It opens files from the record's base commit, where the branch leaves the depot: a diff from the last included
  commit would replay earlier updates' renames and deletes against what p4 has opened. `plan_update` refuses `-b` unless
  it replaces a base that no longer resolves. A shelving update first runs `check_at_latest_revision` over the files
  since there and the files opened in the CL, unless `--ignore-out-of-date` or a step. `--per-commit` instead updates
  once per commit after the included ones through `run_per_commit`, with `shelf_delay` between shelving steps, each step
  without `-b`, so it opens files from the record's base commit too. Requires a clean git workspace unless `--no-edit`
  is given.

- **`included.py`** - The commits a changelist includes: `new_record` for `new`, and `plan_update`, which checks the
  included commits' identities (`Backend.commit_identities`), finds dropped ones, and works out where `update` starts
  and what it appends. A recorded base that no longer resolves (`resolve_recorded_base`) is an error, unless `-b` is
  given, which then becomes the recorded base. `catch_up_subject` says whether a base moved since the last shelve needs
  an empty catch-up commit, named after the base's last sync and the cherry-picks since it (`sync_state`), which
  `update` inserts through `Backend.insert_empty_after` before planning again.

- **`review`** - Has no module: its parser runs `new_command` as `new --review --per-commit`.

- **`list_changes.py`** - The `list-changes` command: prints the numbered commit subjects since a base branch.
  `--porcelain` sets the parser's `quiet`, as `alias show` does, so `run_command` runs it under `log.quiet` and only
  the list, printed directly, reaches stdout.

- **`alias.py`** - The `alias` actions (`list`, `show`, `new`, `rename`, `delete`, `clean`), each an `alias_*_command`
  built on `changelist_store.py`. `new` refuses a CL `update` can't edit through `lib.check_own_pending`. `rename` takes
  one or two names, as `git branch -m` does, and resolves the `branch` keyword for each itself: the current alias as the
  old name, the suggested one as the new. `clean` deletes the aliases whose changelist `is_changelist_gone` finds
  submitted or missing, or whose file holds no changelist (`unreadable_aliases`), after giving the aliases an older
  version saved in another case or under a now reserved name a free lowercase one (`lowercase_aliases`,
  `migrate_changelist_aliases`); it and `delete --all` go on past an alias that fails to delete, then raise
  `CommandError` with the count. `clean --porcelain` sets `quiet`, as `list-changes --porcelain` does, and prints one
  line per alias directly.

- **`init.py`** - The `init` command: checks it runs in a Perforce workspace, then asks for the depot root, writable
  mode, split users, the Claude Code skill and, with jj 0.41 or newer, colocating jj and the `jj p4son` alias. It sets
  up `.gitignore` (from `.p4ignore` if present), creates the git repo with a first commit, records `[sync] branch` (in a
  new repo the branch `HEAD` is on; in an existing one `main` first, then `HEAD`'s branch, then a menu), runs `jj git
  init --colocate` last if asked to in an existing repo (a new one lists it as a next step, after the user's first
  commit).

- **`writable.py`** - Writable mode: reading and writing the `core.writable` setting and the `writable` command (shows
  the mode without an action; `enable`, `disable`, `apply`). Its helpers (`make_writable`, `make_read_only`,
  `restore_writable`, `is_user_writable`) set and clear the user write bit on git-tracked files and are also used by
  `sync` and `lib`. Making files read-only skips files opened in Perforce. `check_not_allwrite` refuses a client spec
  with the allwrite option, which git-p4son does not support, for `writable`'s off path, `apply` and `disable`, and,
  through `sync.sync_settings`, for `sync` and `cherry-pick`.

- **`skill.py`** - The `skill` command: `install` writes a stub `SKILL.md` to the user's Claude Code skills directory,
  and `show` prints the full agent instructions shipped in `skill/git-p4son.md`, followed by the workspace's own
  overrides in `.git-p4son/skill-override.md` when run inside a workspace that has one. The stub only tells the agent
  to run `skill show`, so the instructions follow the installed version. `init` offers the install.

## Command support

- **`sync_merge.py`** - Saves what a sync would overwrite and merges it back: `prepare_writable_files` sorts the files
  p4 would touch that are writable or changed by a commit other than a sync (read-only after `writable off`), into
  tracked and ignored ones, stages the locally modified ones, and `merge_changed_files` merges those local changes back
  after the sync, one file at a time (`_merge_back`), telling `on_merged` after each, with an interrupted run's outcomes
  (`merged_before`) reported beside them, each file once, under its last outcome that leaves something to look at, else
  its last, as a later clean merge carries an earlier one's conflict markers along (`_by_outcome`);
  `stage_changed_files` stages a local change (`ChangedFileRef`) from the pre-sync commit and its base, for a pass and
  for a resume. `merge_intersync_changes` applies the changes made while a sync was interrupted (`IntersyncChange`) onto
  the files as they then stand: a 3-way merge for text, the user's version for binary, an add written, a delete applied
  to an unchanged file and kept for a changed one, and an edit to a file Perforce deleted restored, reported with an
  interrupted run's intersync outcomes under a heading of their own. `report_not_synced` lists the git-ignored writable
  files p4 refused or overwrote.

- **`sync_progress.py`** - The sync progress: what an interrupted sync has left to do, in
  `.git-p4son/sync-progress.toml` (`version = 1`), written to a temporary file and renamed into place, retried a few
  times while Windows refuses the rename. A `SyncProgress` holds the git-p4son that wrote it, the start time, the sync
  branch, the depot root, the recorded tip, the pre-sync commit, the backend's `[start]` (`Backend.progress_start`), the
  passes left (`ProgressPass`), the commits a resume unwound (`unwound`, each an `AboveCommit`: the commit, how to get
  it back, its subject and, with jj, its change ID, which tells a change apart across the snapshots that rewrite it,
  kept by `INTERSYNC_COMMITS_REF`), the files the passes made since the last commit synced (`uncommitted`, a catch-up
  pass's until the next commit), the first one with the `PassLists` its preview and `prepare_writable_files` gave (the
  files with their modes, the local changes, the ignored files), and, in `[end]`, what the end needs from the passes
  made: the local changes still to merge back, the outcome of each one merged (`MergedFile`), and apart from them each
  intersync change's (`intersync_merged`), the intersync saves (`IntersyncSave`, kept alive by `INTERSYNC_REF`: the
  commit, each file with its base, `have` or a commit, and whether its `have/` content is kept yet, which
  `ProgressRecorder.intersync_merged` drops one by one), the ignored files and, in writable mode, the files synced.
  Local changes are references (`LocalChange`: a path, its base commit and flags), read again from the pre-sync commit
  and the base. Paths are slash paths from the workspace root, a list item per line, for a user to read.
  `ProgressRecorder` keeps it in step as `run_sync_passes` and `_resume_sync_passes` go; `read_sync_progress` reads it
  back for a resume, refusing one a newer git-p4son wrote, `check_no_interrupted_sync` refuses `new`, `update`, `review`
  and `cherry-pick` while it exists, as the files on disk are partly synced. `local_path` and `local_paths` turn stored
  paths into local ones, and `workspace_path` a local path into a stored one, for the other modules too. `GIVE_UP` names
  `sync --abort` beside each way to finish an interrupted sync, and `StaleSyncProgress` is the stale refusal, which an
  abort catches.

- **`per_commit.py`** - `run_per_commit` runs a git p4son command on each commit since a base branch, in-process, with
  that commit checked out through the backend, for `new --per-commit` and `update --per-commit`. The steps skip
  `run_command`, so its clean check runs once, before them. A step's arguments are parsed with the real parser just
  before it runs, with the base resolved once to a SHA and given as `-b` to the steps `base_to` names. It returns to
  where it started, also when a step fails, ends the error with the command that resumes after the last commit that
  worked, and sleeps the delay its caller gives between steps. `base_name`, the base's name, goes to every step, as
  the steps get the base as a commit.

- **`processes.py`** - Blocking processes: reading the `[sync] blocking-processes` setting, listing running
  processes (`ps` or `tasklist`), and `check_no_blocking_processes`, which `sync_preflight` runs first.

- **`hooks.py`** - Hooks, the commands of `pre-sync` and `post-sync` in `[hooks]` in `config.toml`: `hook_commands`
  reads a hook's commands, each checked as a configured command with the sync variables (`$(synced)` for `post-sync`
  only), and `run_hook` runs them in order from the workspace root through `command_line`, with `$(backend)` for the
  backend, counting the failures without stopping at one. `check_hooks`, which `sync` and `cherry-pick` run first,
  checks both hooks after `hooks_migration.check_no_old_hooks`. `sync` runs the `pre-sync` hooks in `sync_on_branch` and
  the `post-sync` ones in `sync_on_branch`, with `SyncState.latest` as `$(synced)`.

- **`hooks_migration.py`** - The hook setup of earlier versions, kept apart to be deleted whole one day:
  `check_no_old_hooks` refuses a file in `.git-p4son/hooks/<hook>/` that no `[hooks]` command names, after printing the
  hook's `[hooks]` lines, its commands followed by one per such file with the interpreter it ran with (the script
  runners on Windows, the file itself elsewhere). A file that never ran only gets a warning. Then it refuses the script
  runner settings, `[core.script-runners]` and `[hooks.extension-associations]`.

- **`sync_targets.py`** - Sync targets: the commands under `[sync.targets]`, by name, that print a changelist
  (`sync_target_names`, checked as a whole whenever they are read), `[sync] default-target` (`default_sync_target`),
  and `resolve_sync_target`, which runs a target's command from the workspace root with `common.run_for_output`,
  `$(last-synced)` and `$(depot-root)` filled in, and parses the changelist it prints.

- **`configured_command.py`** - The command lines a workspace configures in `config.toml`: checking a command given as a
  list (`check_command`) or a table with argument lists (`check_command_table`, with `check_keys`), and its `$(name)`
  variables, and `command_line`, the command to run: its variables expanded, the caller's values first, then the
  Perforce ones and the environment, and a relative program joined to the workspace root. `SYNC_VARIABLES` and
  `sync_values` are the variables sync targets and hooks share, `$(last-synced)` and `$(depot-root)`.

- **`review_tools.py`** - Review tools, and every decision about what Swarm means: Swarm (`SWARM`), built in, and the
  tables under `[review-tool]` in `config.toml` (`review_tool_names`). `resolve_review_tool` picks `--review-tool`, else
  `[review-tool] default` (`default_review_tool`), else Swarm, and refuses an unknown tool through `check_review_tool`;
  `_section` refuses a stray key in `[review-tool]` and a `swarm` or `default` table. `runs_command` is whether a tool
  is run after a shelve: any but Swarm, which takes the shelve itself. `review_tool_config` checks a tool's `command`,
  `new` and `update` lists and their `$(name)` variables. `shelf_delay` is `SWARM_SHELF_DELAY` for Swarm and none for a
  tool that is run. `run_review_tool` runs the command with the `new` arguments until a run has succeeded and the
  `update` ones after, with the variables expanded and a relative program joined to the workspace root, as Windows
  ignores `cwd` when it looks for the program, through `run_interactive` from the workspace root, and raises
  `CommandError` when it fails; `push_review` then marks the alias started. `review_tool_command` is the `review-tool`
  command: `list`, `default`, which writes `[review-tool] default`, `set`, which records a tool in an alias as started
  (`set_review_tool`), `show` (`alias_review`), and without an action the list and the current alias's tool, if the
  branch keyword names an existing alias.

- **`complete.py`** - Shell completion: `run_complete` walks the argparse parser to suggest commands, flags, actions,
  aliases, split users, review tools and the `branch` keyword. Logging is silenced so stdout carries only candidates;
  the scripts in `completions/` call `git p4son complete`. Their `git-p4son-jj.*` hooks, which `completion --jj`
  prints, complete `jj p4son` and hand every other `jj` completion to jj's own; `tests/test_completion_scripts.py`
  runs them in each shell.

- **`jj.py`** - Colocated jj workspaces. `operation` pins a jj operation and holds jj's working-copy lock while a
  command runs, yielding a `Workspace`, or None outside jj and with `lock_only`, which the git backend uses and which
  needs no jj. The `Workspace` resolves the change git-p4son works on (the working change, or its parent when the
  working change is empty and new), its bookmark and the default base; commands reach it only through `JjBackend`. git
  still reads objects, diffs and makes the sync commits, and `Workspace.checkout` and `restore_working` move the files
  on disk for sync under the lock; `rebase_branch`, `conflicted_since`, `working_is_empty` and `return_to` run jj on the
  live working copy, only after it. `operation` warns when git's `core.autocrlf` (`git.get_autocrlf`) and jj's
  `working-copy.eol-conversion` disagree, and about files the tip adds that git ignores only by matching without case
  (`check_ignored`, which `colocate` also runs, and which names the `jj config set` and `jj file untrack` that fix
  it). `leave_out_case_only_ignored` leaves those untracked folders out of jj's `snapshot.auto-track` for the repo
  before jj snapshots them, in the jj backend's `finish_sync` and `fail_sync`; `colocate` and `colocate_steps` pass
  the setting to `jj git init`. `check_invoked_inside_workspace`, which `run_command` calls first, refuses `jj -R`
  naming another workspace through `JJ_WORKSPACE_ROOT`, then drops the variable so hooks don't inherit it. For
  `init`: `is_supported`, `colocate` and `colocate_steps`, which match git's line endings from jj's first snapshot on,
  `has_p4son_alias` and `add_p4son_alias`.

- **`jj_lock.py`** - Takes jj's local working-copy lock the way jj does, with `lock.py`'s `file_lock`. Hooks must
  not run jj commands that snapshot, and a git-p4son run under its caller's lock needs `--backend git`.

- **`lock.py`** - `file_lock` locks a file with `flock` on POSIX and `LockFileEx` on Windows, as jj locks its working
  copy. A busy lock raises `LockBusy`, a `CommandError`, instead of waiting. `command_lock` is git-p4son's own lock on
  a git workspace, on `.git-p4son/lock`. `marked_as_locked` marks a held lock with a token, in the environment and in
  `.git-p4son/lock-holder` until the lock is released, so a git-p4son its holder starts, as a hook does, finds its
  caller holding the lock (`locked_by_caller`) and takes none, while a process that outlives the holder takes the
  lock like any other. Both locks trust the mark only while the lock is busy.

## Shared modules

- **`backend.py`** - The backend: what a command asks of the tool holding the change, as the `Backend` interface: the
  commit to read the change from (`revision`), the base (`resolve_base`, None for the default, and `implicit_base`, the
  one `new` and `review` take without `-b`), the alias the branch or bookmark suggests, the jj change aliases record
  (`change_id`, `existing_changes`), the default message, the clean check (`check_clean`, which sync shares), the check
  that the sync branch exists and the change contains its tip, with no merge above it (`check_on_sync_tip`), the check
  that a base contains it too and is below the change (`check_base`), `-b` completions, `checkouts` for the per-commit
  loop, sync's commits (`prepare_sync`, `start_sync`, `commit_sync`), a resume's check of where the interrupted sync
  left the sync branch, taking the start from the sync progress (`resume_sync`), the commits above the tip a resume
  sorts (`commits_above`), where the resume puts the sync branch for its passes, the recorded tip or the crash window's
  commit, with the user's commits unwound (`resume_at`), the files changed since a commit (`files_changed_since`), where
  the sync started for the sync progress (`progress_start`: git's branch or detached commit, jj's start change and
  whether it was edited), and the `Words` its messages use, whose `tool` hooks get as `$(backend)`. `selected_backend`
  picks git or jj from `--backend`, then `core.backend`, where `auto` means jj in a jj workspace. `open_backend` holds
  the workspace's lock for the whole command, jj's lock and operation in a jj workspace and `command_lock` in a git one,
  and yields the backend in `run_command` as `args.backend`; jj snapshots the working copy first unless the command's
  parser sets `snapshot=False`. Once the lock is released, it calls the backend's `after_lock`. `alias_for_branch`
  resolves the `branch` keyword: the alias recording the change, or the change `nearest_change` finds below it in its
  chain, else the suggested name, unless the command (`new_alias=True` on its parser) names a new one;
  `resolve_branch_keyword` does so under a heading, for `run_command` and `alias rename`. `completion_backend` gives one
  without the lock, and `workspace_lock` is `init`'s lock-only path. No command branches on jj outside the backend
  modules and `jj.py`, apart from `init`'s offer to set jj up.

- **`git_backend.py`** - `GitBackend`, the git adapter: `HEAD`, `HEAD~1` as the default base, a branch as the base by
  its full ref, so that a tag of its name isn't taken for it, the nearest branch below HEAD down to the sync branch
  as `new`'s and `review`'s (`git.nearest_branches_below`), the current branch, `checkouts` detaching at each commit
  and returning to the starting branch, and the sync branch: `--branch`, else `[sync] branch`, else `main`, checked
  out for the sync and returned from with a `git rebase` hint.

- **`jj_backend.py`** - `JjBackend`, the jj adapter, delegating to a `jj.Workspace`. `new`'s and `review`'s base without
  `-b` is the one nearest bookmark below the change (`nearest_bookmarks`). `base_name` records a base without a bookmark
  as `change_id(<ID>)` (`jj.changes_revset`), which resolves the change's current commit. Its sync refuses a sync
  bookmark at the working change (`prepare_sync`), checks out the bookmark, commits with jj's line endings and moves the
  bookmark; `after_lock`, which `open_backend` calls once jj's lock is released, runs `jj rebase -b <start> -d
  <bookmark>` (unless `--no-rebase`), lists conflicts, and returns with `jj edit` or `jj new` unless merged-back changes
  are left. A resume (`resume_sync`) wants the bookmark at the recorded commit, or on the pass's own commit above it
  where jj's `commit_sync` moved it before a crash, and the working change above it, not on the stack sync started from,
  which sits on the recorded commit too until a pass commits, the described changes between being the user's commits, a
  bookmark moved onto later sync commits refusing as stale sync progress, and `resume_at`, once the dry run and the
  pre-sync hooks are past, moves the bookmark to the crash window's commit where the interruption left it behind, keeps
  that commit's change from the snapshot changes, and points git's HEAD at the commit. `after_lock` then abandons the
  undescribed snapshot changes jj made there, once jj is past them, and the user's described changes it unwound; one
  that is still the working change, as no pass was left to commit, has its description cleared instead
  (`Workspace.undescribe_working`), and a start change that is gone leaves the sync on the bookmark with a warning, as a
  gone start branch does with git. Its `checkouts` puts each commit's files on disk with git under the lock, as the
  revision and clean check a step reads, and the working change's files back at the end, so jj sees no change.

- **`lib.py`** - Combines git and Perforce operations: `check_git_workspace_clean`, `check_own_pending` (refuses a CL
  `describe_changelist` doesn't find pending and owned by the current user and client, `get_p4_user_and_client`,
  ignoring case, which `alias new` and `update` run), `create_changelist`, `update_changelist`,
  `include_changes_in_changelist` (returns how many files it opened, moved or reopened; one
  `perforce.get_opened_files_and_moves` query gives what is opened and each move's other end, by depot path when git's
  change doesn't name it, `_plan_moves` sorts git's renames into one batched `p4 move` and leaves a move already open to
  `_plan_opens`, and a reverted move end takes its other end along, as p4 won't revert a move/delete alone; then
  `_check_opened` asks p4 again and raises `CommandError` for any file it didn't open, as p4 exits 0 for most, so no
  shelve goes out without it), `revert_unchanged_files`, `edit_and_shelve`, which `new` and `update` share, and
  `check_at_latest_revision`, which lists and refuses the files `perforce.get_out_of_date_files` finds behind the head
  revision before a shelve. They read the change from the backend's `revision` and revert with `p4 revert -k`, so p4
  never writes over a working file. On a dry run of `new` the changelist is `DRY_RUN_CHANGELIST`.

- **`description.py`** - Models a changelist description (`Description`): the message, the enumerated commit list
  under `Changes included:`, trailing text, and Swarm `#review` keywords on lines of their own. `parse` and `format`
  own the layout; `new` and `update` build descriptions through it.

- **`changelist_store.py`** - Changelist aliases stored in `.git-p4son/aliases/<name>.toml`, names lowercased, as
  aliases ignore case: save, load, list and delete, rename (`rename_changelist_alias`, which moves everything an alias
  holds), name validation, the reserved keywords (`last-synced`, `head`, `branch`) and `branch_alias` for the branch
  keyword. An alias made by `new` holds a `Record`: the base's name, its commit, and the identities of the commits since
  it the CL includes, and an inserted catch-up commit until it is included (`find_record`, `update_record`). An alias
  may record a jj change (`change = "..."`), at most one alias per change, as saving moves the record:
  `alias_for_change`, `aliases_by_change` and `alias_changes`. An alias can record a review tool (`review_tool`) and,
  once the tool has run for the CL, `review_started` (`find_review`, `alias_review`, `set_review_tool`,
  `mark_review_started`). `_write_alias` writes a whole `_Alias`, so every write keeps them. The old
  `.git-p4son/changelists/<name>` store is still read; writes move an alias out of it, and `migrate_changelist_aliases`,
  which `alias clean` runs first, moves the rest.

- **`sync_branch.py`** - The sync branch: `[sync] branch` (`get_sync_branch_setting`), `DEFAULT_SYNC_BRANCH`,
  `sync_branch_name`, the setting else `main` without checking it exists, and `choose_sync_branch`, the `--branch`,
  setting, `main` order both backends and `init` share.

- **`depot.py`** - Owns the depot root: reading it from config, expanding the `$(workspace)` placeholder, and
  `resolve_depot_root`, which returns it with the client spec (`ResolvedDepot`) for `sync`. `check_depot_root` and
  `subdirectory_depot_root` back `init`'s choice.

- **`config.py`** - Per-repo configuration in `.git-p4son/config.toml`. `get_setting` reads a typed value with a default
  and raises `ConfigError` for a wrong type or invalid TOML. `set_setting_in_place` writes every setting git-p4son
  writes: it finds the setting's table and key as TOML reads them, in whichever form they're written (a table, a dotted
  key, an inline table, which is rewritten from its parsed entries), replaces its value over every line it spans,
  keeping the comments on those lines (the ones inside a value move above its key), and leaves the rest of the file as
  written. It refuses, leaving the file alone, a file that isn't valid TOML, with `load_config`'s error, and a setting
  the file can't take without replacing what it holds there, such as a table at its key. Holds the depot root,
  `core.writable`, the split users, and the blocking processes. The review tools, sync targets and hooks read their own
  settings.

- **`toml_writer.py`** - `format_toml`, the minimal TOML writer behind the alias files, and `format_value`, which
  `set_setting_in_place` formats a `config.toml` value with, escaping control characters in strings; reading uses
  `tomllib`.

- **`git.py`** - The git layer: workspace discovery, dirty and staged files, `get_local_changes` (adds, edits, deletes
  and moves since a base), commit subjects, staging and commits, the sync and cherry-pick commit subjects
  (`format_sync_subject`, `format_cherry_pick_subject`; `git_last_synced_changelist` skips cherry-picks, and
  `sync_state` gives the last sync and the cherry-picks since it as a `SyncState`), blob reads,
  `files_changed_outside_syncs` (the tracked files other commits changed since the first sync) and `merge_file` for
  `sync_merge`, and `added_files`, `ignored_files`, `case_only` and `case_only_ignored`, which compares two `ls-files`
  walks, for jj's ignore check. For a resume: `files_changed_since`, `commits_above`, `is_sync_subject`,
  `reset_branch_keeping_files`, `commit_files` (a commit of local files at tree paths of its own, through a temporary
  index), `set_ref`/`delete_ref`, and `restore_files` (files as a commit holds them, the real index left alone). git's
  `-z` output is read as bytes.

- **`perforce.py`** - The Perforce layer, with every p4 call pinned to English output (`-L en`): ztag parsing, the
  client spec (`P4ClientSpec`, `ClobberMode`), local paths as file arguments (`p4_file_arg`, which escapes `@#%*`, for
  every `-x -` list through `_run_file_list`, `p4_off_have` (`p4 diff -se`) and `p4_sync_to_have`
  (`p4 sync -f <file>#have`, a revision after each escaped file) and `p4_sync_pending` (`p4 sync -n` over files) among
  them; `is_p4_temp_file` tells the `tmp.<pid>.<hex>.<n>` names p4 writes a file under before renaming it into place;
  `add -f` takes them raw), changelist specs, opened files and batched commands (`run_p4_batch`), reverts, shelving,
  submitted changes and users, a changelist's status and owner (`describe_changelist`) and whether it is gone (one
  `describe` each, as a batched one skips the changelist after a missing one), and `p4_sync`/`p4_sync_preview` with
  `P4SyncOutputProcessor` parsing streamed progress (`parse_p4_sync_entry`) and raising `P4SyncClobberError` or
  `P4SyncAbortError`.

- **`common.py`** - Shared utilities: the `CommandError` and `RunError` exceptions, subprocess execution (`run()`,
  `RunResult`), the first bytes of a command's output, ending it once read (`run_output_start()`, for `p4 print`),
  real-time output streaming via threading (`run_with_stream_output()`, used by `p4 sync`, which `_stop` interrupts on
  an error or Ctrl-C with SIGINT, or on Windows with a Ctrl-Break to a process group of its own, and kills after 10
  seconds or on a second Ctrl-C), commands run on the terminal (`run_interactive()`, used by review tools), commands
  whose stdout is read while their stderr shows (`run_for_output()`, used by sync targets), `normalize_workspace_path`,
  `restoring` (the backends' `checkouts` return through it), and the interactive prompts `prompt_choice`, `prompt_menu`
  and `prompt_numbered`.

- **`log.py`** - All user-facing output goes through the `log` singleton: headings, success, warning and error lines,
  file changes, command echo with a spinner, child process output, and quiet and verbose modes, with
  `log.quiet()` scoping quiet mode to a block. Writes to stderr flush stdout first, so merged streams keep their order.
  A line logged while a command line is open, as from a streamed command's output handler, first ends that line and
  stops its spinner (`stop_spinner`). `plural` formats counts.
