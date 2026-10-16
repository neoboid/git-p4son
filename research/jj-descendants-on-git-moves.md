# What jj does to descendants and bookmarks when git moves a branch

Research for wayfinder ticket `04-jj-descendants-on-git-moves`. Researched 2026-10-16 against jj 0.46.0 (latest,
released 2026-10-07), with spot checks on jj 0.41.0, the minimum git-p4son accepts. Source links point at the
`v0.46.0` tag.

## Short answer

- **Today's `sync` is a fast-forward of `main`, and jj leaves every descendant of the old `main` where it is.** No
  rebase, no divergence, no abandon. Feature changes stay on the old `main` commit, which stays visible as an
  ordinary ancestor of the new one.
- **The working change `@` is replaced, not rebased.** Because `sync` also moves git `HEAD`, jj's next snapshot sees
  a new git HEAD, checks out a fresh empty `@` on top of it and abandons the old `@` because it is discardable (empty,
  no description). A non-discardable old `@` would be left behind on the old `main` as a plain visible change.
- **The bookmark `main` simply follows `refs/heads/main`**, recorded as two operations attributed to whichever jj
  command imports first (in git-p4son, the reconcile `jj status`): `import git head`, then `import git refs`.
- **jj rebases descendants on import only when a commit is rewritten or abandoned**, never when a bookmark moves
  forward. Since 0.43.0 a rewrite is detected by change id; a plain `git commit` has no change-id header, so `sync`'s
  commits never trigger it.
- **No jj command moves a bookmark and rebases descendants in one operation.** `jj bookmark set/move` never rebase.
  The one-operation options are `jj rebase` after the import, or building each sync commit with jj itself via
  `jj new --insert-after main`, which rebases `main`'s children as part of creating the commit.

## What git-p4son does today

On `minimal-jj-rebased` (PR #115), `git_p4son/jj.py` and `git_p4son/sync.py`:

- `Workspace.prepare_sync` requires `@` to be empty, undescribed, unbookmarked and single-parent, its parent to carry
  the sync bookmark, and git `HEAD` to equal that parent.
- `detach_for_sync` runs `git update-ref --no-deref HEAD <tip> <tip>`, so git commits do not advance a branch.
- For each changelist `sync` makes a git commit, then `advance_sync` runs
  `git update-ref refs/heads/<bookmark> <new HEAD> <old tip>`.
- All jj calls inside the command use `--ignore-working-copy --at-op <op>`. With `--ignore-working-copy`, jj skips
  the snapshot and therefore also skips both git imports (`snapshot_impl` and `maybe_snapshot` in
  [`cli/src/cli_util.rs`][cli_util]). The first importing command is the `jj status` in `jj.operation`'s
  "Reconciling jj workspace" step, after the lock is released.

## How the colocated import works (jj 0.46.0 source)

Every snapshotting jj command in a colocated workspace runs, in order ([`snapshot_impl`][cli_util], around line
1326):

1. `import_git_head`: if git HEAD differs from the HEAD jj last recorded, check out the new HEAD as the parent of a
   new working-copy commit, reset the working-copy state without touching files, and abandon the old working-copy
   commit "if it's discardable". Prints `Reset the working copy parent to the new Git HEAD.` and commits an
   operation named `import git head` ([`import_git_head`][cli_util], around line 1380; [`import_head`][git_rs],
   around line 1182).
2. Snapshot of the working copy.
3. `import_git_refs`: diff git's refs against jj's view, move local bookmarks to the new git targets, then possibly
   abandon or rewrite commits (below), then `rebase_mutable_descendants`. Prints
   `Rebased N descendant commits off of commits rewritten from Git.` when it rebased anything, and commits an
   operation named `import git refs` ([`import_git_refs`][cli_util], around line 1457).

In `import_refs` ([`lib/src/git.rs`][git_rs], around lines 640-780) two mechanisms can make descendants move:

- **Abandon unreachable** (`git.abandon-unreachable-commits`, default `true`, [config docs][config_abandon]): commits
  that were reachable from the old ref targets, are no longer reachable from any ref, and are not pinned by another
  local ref are abandoned; their descendants are rebased onto their parents "as usual when commits are abandoned".
  Note that visible jj heads pin nothing here; only refs do (`abandon_unreachable_commits`, around line 786).
- **Synthetic predecessors by change id** (`git.record-synthetic-predecessors`, default `true`,
  [config docs][config_synthetic], [`misc.toml`][misc_toml]): for commits that fell out of the old ref targets'
  history and new commits that entered it, jj matches change ids. A unique match records the old commit as rewritten
  into the new one, and descendants are rebased onto it; several matches record a divergent rewrite
  (`record_synthetic_predecessors`, around line 815). The [0.43.0 changelog][changelog] introduced the rebasing:
  "`jj git fetch` now rebases the descendants of revisions that were rewritten based on their change IDs."

A fast-forward triggers neither: nothing falls out of the history, so nothing is abandoned and nothing is matched.

Change ids reach git through a `change-id` commit header written by jj (since 0.30.0). Per the
[git compatibility docs][git_compat], `git commit --amend` keeps the header and a git rebase drops it. A plain
`git commit`, as `sync` makes, has none, so jj derives a fresh change id from the commit id.

## Experiments

Throwaway colocated repos under `/private/tmp/claude-501/jjexp/`, with jj 0.46.0 unless noted. "p4sync" means
exactly what `sync` does: detach HEAD with `update-ref --no-deref`, `git commit`, `git update-ref refs/heads/main`,
once per changelist; then `jj status`.

### 1. Today's sync, fast-forward of two changelists (0.46.0 and 0.41.0 agree)

Before: `A` (main), `feature` (bookmark `feat`) on `A`, empty `@` on `A`. After p4sync of CL 2 and CL 3, `jj status`
printed `Reset the working copy parent to the new Git HEAD.` and `Done importing changes from the underlying Git
repo.`, and:

```
@  xszusklu (empty) (no description set)
○  tnnpznzl main  CL 3
○  wsmtxoqz       CL 2
│ ○  xsqsykks feat  feature
├─╯
○  rursprvz       A
```

- `feature` stayed on `A`: not rebased, not divergent.
- The old empty `@` was abandoned; a new empty `@` (new change id) sits on `CL 3`.
- `jj op log` shows `import git head` then `import git refs`, both with `args: jj status`. `jj op show` on the
  latter lists `Changed local bookmarks: main: + CL 3, - A`. The intermediate CL 2 bookmark position is never
  recorded, since jj only sees the final ref.
- git stays on a detached HEAD at `CL 3`.

### 2. Non-discardable `@` and an anonymous change on the old main

A described `@` with file content (`wip`) and an anonymous change `anon`, both on `A`. After p4sync of CL 2: both
stayed on `A` untouched; jj created a new empty `@` on `CL 2`. The old `@` is no longer the working copy, but it is
not lost. (`prepare_sync` refuses this state today; this shows what a relaxed precondition would get.)

### 3. git rewrites main with a plain commit (non-fast-forward, no change-id header)

`main` at `B` (git-made), `feature` on `B`. Replace `B` with `B'` by `git reset --soft HEAD~1 && git commit`. Result:
`main` moved to `B'`, `B` and `feature` stayed where they were. `B` was not abandoned because `feat` still reaches
it. Nothing was rebased; no divergence (different change ids).

### 4. git amends a jj-made commit at main, moving HEAD (change-id header kept)

`main` at jj-made `B`, `feature` on `B`. `git commit --amend` with HEAD on `B`, then move `main`. Result: **divergent**
change, `B/0` (new, `main`) and `B/1` (old, still parent of `feature`). Nothing rebased. The HEAD import runs
first and makes `B'` visible, so the refs import no longer sees it as newly imported and does not record it as a
rewrite.

### 5. Same rewrite, HEAD not moved (0.46.0 versus 0.41.0)

As 4, but `B'` is written with `git hash-object` and only `refs/heads/main` moves. jj 0.46.0 printed `Updated 1
rewritten commits.` and `Rebased 2 descendant commits off of commits rewritten from Git.`: `feature` and `@` were
rebased onto `B'`, no divergence. jj 0.41.0 made `B` divergent and rebased nothing. So the change-id rewrite on
colocated import is new in 0.43.0 (0.42.0 untested) and depends on git HEAD not moving to the rewritten commit first.

### 6. jj-side follow-up: one rebase after today's sync

After experiment 1's state (with a second anonymous `feature2` on `A`):

```
jj rebase -s "children($OLD_MAIN) ~ ::main ~ @" -d main
```

moved `feature` and `feature2` onto `CL 3` as one operation (`rebase 2 commits and their descendants`). `jj git
import` here only says `No import needed in colocated workspaces.` but its snapshot still did the two imports.

### 7. jj-native sync commit: `jj new --insert-after main`

```
jj new --insert-after main -m "CL 2"   # rebases main's children (feature, @) onto the new commit
<p4 sync writes the files>             # stood in for by writing f
jj bookmark set main -r @              # snapshot first: amends CL 2, rebases its descendants again
jj new
```

Result: `feature` on `CL 2`, `main` at `CL 2`, empty `@` on top, old empty `@` abandoned, git detached at `CL 2`.
Four operations (`new empty commit`, `snapshot working copy`, `point bookmark main`, `new empty commit`). The commit
is jj-made, so it carries the jj user as author and a `change-id` header; descendants are rebased onto each
changelist in turn, recording conflicts if a changelist touches their files.

## Answers to the ticket's questions

- **Descendants of the old main**: left in place. Rebasing happens on import only when jj infers a rewrite (change id
  match, 0.43.0+) or abandons an unreachable commit; a fast-forward does neither. Divergence appears only when git
  rewrites a jj-made commit and keeps its change id while HEAD also moves (experiment 4).
- **Bookmarks**: the local bookmark follows the git branch to its final target in one `import git refs` step. Other
  bookmarks (`feat`) do not move.
- **`@`**: a new empty working change on the new git HEAD, made by `import git head`. The old `@` is abandoned if
  discardable, else left visible on the old main.
- **Operation log**: two operations, `import git head` and `import git refs`, attributed to the first snapshotting
  jj command after `sync` (in git-p4son, the reconcile `jj status`). No operation records git-p4son's own steps or
  the intermediate changelist positions. `jj undo` / `jj op restore` can roll the import back (per the
  [git compatibility docs][git_compat]; not tried here).
- **One-operation jj alternative**: none that both moves a bookmark and rebases. `jj bookmark set/move` has no
  rebase option (checked `jj bookmark move --help`, 0.46.0). Candidates:
  - keep the git commits and run one `jj rebase -s <roots> -d main` after the import (experiment 6); it is a jj
    command that needs the working-copy lock, so it would run in or after the reconcile step;
  - make each sync commit with jj via `jj new --insert-after <bookmark>` (experiment 7), which rebases on every
    changelist, changes authorship and needs `sync_merge.py`'s git-index-based merge rethought.
  Which descendants to rebase (all mutable descendants of the old main, only bookmarked ones, none) is a design
  decision for the jj-native sync ticket, not something jj decides.

## Version caveats

- Fast-forward behaviour (experiments 1, 2) was identical on 0.41.0 and 0.46.0.
- Change-id based rewrite detection on import, and the rebase of descendants it causes, is 0.43.0+ per the changelog;
  0.41.0 produced divergence instead (experiment 5). 0.42.0 was not tested.
- 0.45.0 changed git HEAD tracking to per worktree ("Git HEAD state is now tracked per worktree internally"),
  preparing colocated multi-workspace support, which 0.46.0 adds (`jj workspace add --colocate`). Experiments used
  only the default workspace.
- `git.abandon-unreachable-commits` and `git.record-synthetic-predecessors` are user-configurable; a user who turned
  them off would see no rebases from either mechanism.

## Sources

- [`lib/src/git.rs` at v0.46.0][git_rs]: `import_refs`, `abandon_unreachable_commits`,
  `record_synthetic_predecessors`, `import_head`.
- [`cli/src/cli_util.rs` at v0.46.0][cli_util]: `snapshot_impl`, `import_git_head`, `import_git_refs`.
- [`lib/src/config/misc.toml` at v0.46.0][misc_toml]: defaults for the `[git]` settings.
- [Configuration docs][config_abandon]: sections "Abandon commits that became unreachable in Git" and
  "Replicating evolution history for fetched or imported Git commits".
- [Git compatibility docs][git_compat]: colocated workspaces, change-id header.
- [CHANGELOG at v0.46.0][changelog]: 0.43.0 change-id rebasing, 0.45.0 per-worktree HEAD, 0.46.0 colocated
  workspaces.
- git-p4son branch `minimal-jj-rebased`: `git_p4son/jj.py` (`prepare_sync`, `detach_for_sync`, `advance_sync`,
  `operation`), `git_p4son/sync.py` (`_run_sync`).

[git_rs]: https://github.com/jj-vcs/jj/blob/v0.46.0/lib/src/git.rs
[cli_util]: https://github.com/jj-vcs/jj/blob/v0.46.0/cli/src/cli_util.rs
[misc_toml]: https://github.com/jj-vcs/jj/blob/v0.46.0/lib/src/config/misc.toml
[config_abandon]: https://github.com/jj-vcs/jj/blob/v0.46.0/docs/config.md
[config_synthetic]: https://github.com/jj-vcs/jj/blob/v0.46.0/docs/config.md
[git_compat]: https://github.com/jj-vcs/jj/blob/v0.46.0/docs/git-compatibility.md
[changelog]: https://github.com/jj-vcs/jj/blob/v0.46.0/CHANGELOG.md
