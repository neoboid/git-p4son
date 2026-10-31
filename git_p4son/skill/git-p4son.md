# Working with git-p4son

This git repository is also a Perforce workspace. git-p4son keeps the `main` branch in sync with the Perforce depot.
Work happens on git feature branches, which git-p4son turns into Perforce changelists (CLs) and Swarm reviews.

Run commands from anywhere inside the workspace as `git p4son <command>`. Use `-h` for help: git swallows `--help`.

## Ground rules

- **The git repository is local only.** It has no remotes: Perforce is where changes are shared. Don't push, pull,
  fetch or open pull requests. Changes reach others as CLs and Swarm reviews, and other people's changes arrive
  through `git p4son sync`.
- **`main` mirrors Perforce.** `git p4son sync` adds a commit to it for each changelist it syncs. Do your work on
  a branch off `main`. The user may keep small local changes committed on `main`, such as local configuration. When
  a sync touches those files, it merges the local changes on top of the new Perforce content and leaves the result
  as unstaged changes, with conflict markers if they clash. Leave reviewing, resolving and re-committing them to the
  user.
- **Only touch Perforce when asked.** Run `sync`, `new`, `update` and `review` only when the user asks for exactly
  that. Otherwise draft the commits and the CL description, and let the user drive Perforce. Reviews and shelves
  (`review`, `new --review`, `--shelve`) are seen by reviewers in Swarm. Read-only commands are always fine:
  `alias list`, `alias show`, `list-changes`, `writable` and `sync-split-users` without an action, and any command
  with `--dry-run`.
- **Let git-p4son talk to Perforce.** Don't run `p4 sync`, `p4 edit`, `p4 add`, `p4 delete`, `p4 revert`, `p4 shelve`
  or `p4 change` on files git-p4son manages, except to recover as described under "When something refuses".
  Read-only commands such as `p4 describe`, `p4 opened` and `p4 changes` are fine.
- **Writable mode.** When it is on (`git p4son writable` shows it), git-tracked files stay writable, so you edit and
  commit them without `p4 edit`. git-p4son opens the changed files in the CL when it creates or updates one.
- **Git-ignored files belong to Perforce.** Files ignored by git, typically binary assets, are never opened in a CL
  by git-p4son. The user checks them out, locks and submits them in P4V as usual, and opens them in a review CL by
  hand when needed (see "Binary assets in a review").
- **Never submit.** git-p4son has no submit command on purpose: submitting is left to the user, usually in P4V.
- **Keep the workspace clean.** `sync`, `new`, `update` and `review` refuse to run with uncommitted changes,
  untracked files included (`new` and `update` don't check with `--no-edit`). `review` and the other `--per-commit`
  runs check even on a dry run; `sync --dry-run` does not, so a clean dry run doesn't mean the sync will start.
  Commit or stash first, and keep scratch files, like a description file, outside the workspace.
- **Preview first.** `sync`, `new`, `update`, `review` and `alias clean` take `-n/--dry-run`, which prints what
  would happen without touching Perforce or git.
- **Avoid `init`.** It prompts for answers, and setup is the user's job. Never use `alias delete --all`, which
  deletes every alias without asking, unless the user asks for it.

## jj workspaces

Before running commands in a workspace, check for a `.jj` folder at its root, next to `.git`, in each workspace you
work in. Without `.jj`, follow the skill as written.

With `.jj`, git-p4son works on the jj change, unless `.git-p4son/config.toml` sets `backend = "git"` under `[core]`;
check it first. On the git backend, use git as in any other workspace. On the jj backend, read the rest of the skill
with these translations:

- **Run commands as `jj p4son ...`** when jj has the alias, which `init` offers to add: check once with
  `jj config get aliases.p4son`. It runs the same `git-p4son`, but keeps the commands you show the user in jj's
  terms. Without the alias, run `git p4son ...`. Read `git p4son` in the rest of the skill and in git-p4son's
  messages as either.
- **A branch is a bookmark, and a commit is a change.** Where the skill says to commit, describe the change with
  `jj describe` and start the next one with `jj new`. A git commit leaves the jj change behind, so don't run one.
- **The change git-p4son works on** is the working change `@`, or its parent when `@` is empty, undescribed and new.
  jj snapshots files by itself, so there is nothing to stage or stash. Files jj leaves untracked, usually ones over
  its `snapshot.max-new-file-size`, make commands refuse: tell the user, who either ignores them in `.gitignore` or
  has jj track them. Don't use `git stash`, which moves files behind jj's back.
- **Aliases follow the change.** `new` names the alias after the change's one bookmark, or the first 8 letters of
  its change ID, and records the change, so `update` finds it after rewording or moving bookmarks.
- **`-b` takes a jj revision**, such as `main` or a change ID, and defaults to the change's parent.
- **Branching off `main`** is `jj new main -m "..."` and `jj bookmark create <name>`. **Switching to a change**, as
  `git checkout feature-a` does in the stacks walkthrough, is `jj edit <bookmark>` to amend that change, or
  `jj new <bookmark>` to add a change on top.
- **`sync`** works from any change: it rebases the branch you started from onto the sync bookmark, as
  `jj rebase -b <change> -d main` does, and returns you, so don't rebase that branch after it. A stack moves as one,
  whichever of its changes you sync from. Bookmarks on other branches off `main` stay where they were; bring each up
  to date with `jj rebase -b <bookmark> -d main`. Resolve any conflicts the sync lists with jj.
- **Stacks:** jj rebases the changes above one you edit, so `--update-refs` is not needed. Once A is submitted,
  before syncing, abandoning its changes moves B and the rest of the stack onto A's parent, so there is nothing to
  replay with `--onto`: sync from B, which rebases the stack onto the new `main`.
- **After a submit,** abandon the submitted changes before you sync: `jj abandon main..<bookmark>` drops the
  branch's changes and its bookmark, then `git p4son alias delete` as usual. Ask first, as with git. Synced first,
  the submitted changes are rebased onto their own sync commit and come back empty, or conflicted where a later
  changelist edited the same lines; abandoning them then clears both.
- **Don't add `--backend` yourself.** It switches between the git and jj workflows; ask the user if a command seems
  to need it.

## Where things stand

- `git log --oneline main` shows the sync history. A sync commit has a subject like
  `git-p4son: p4 sync //workspace/...@12345`, where 12345 is the synced CL.
- `git p4son alias list` lists the aliases: names for the CLs that `new` and `review` created.
- `git p4son alias show [alias]` prints only the CL number of an alias. It defaults to the current branch.
- `p4 describe -s <CL>` and `p4 opened -c <CL>` show a CL's description and files.

## Aliases and the branch name

`new` and `review` save the CL they create under an alias, by default the current branch name with `/` replaced by
`-` (`feature/login` becomes `feature-login`). `update` and `alias show` default to the same alias, so on the branch
that created a CL you rarely need to name it. On a detached HEAD, pass the alias or CL number explicitly. Since the
branch name becomes the alias, name the branch after the bug or feature, and keep it unique.

## Branches and commits

Use one branch per bug or feature, off `main`. Commits are what reviewers read: their subjects are copied into the
CL's commit list, and with `review` each commit becomes its own patch in Swarm. So shape them for reviewers:

- Put small, behaviour-preserving refactors first and the actual fix on top, as small as possible. An unrelated fix
  found on the way gets a commit of its own.
- Write the subject as one short sentence saying what the commit does.
- Rewriting history (amend, squash, reorder) is fine until the review is posted. After that, leave the reviewed
  commits as they are: reviewers' comments belong to the posted patches, and `update` matches commit list entries by
  subject. Put every fix in a new commit on top. Rebasing the branch onto a newer `main` is still fine.

### Stacked branches

A big feature can be split into parts that are easier for reviewers to digest, even when the parts change the same
files. Suggest stacking them: part A on a branch off `main`, B on a branch off A, C off B. A goes up for review, and
while it waits, work continues on B locally.

```sh
git checkout -b feature-a main      # commit part A
git p4son review -F /tmp/a.txt -b main
git checkout -b feature-b           # on top of feature-a, commit part B while A is in review
```

- Only the bottom branch is up for review. A review always diffs against what is submitted in Perforce, so posting
  B before A is submitted would show A's changes in B's review too.
- When A gets review feedback, commit the fixes on A and update its CL, then rebase the rest of the stack onto it.
  `--update-refs` (git 2.38 or later) moves every branch in the stack in one go:
  ```sh
  git checkout feature-a              # commit the fixes
  git p4son update --per-commit --shelve
  git checkout feature-c && git rebase --update-refs feature-a
  ```
- After a sync, rebase the whole stack onto `main` the same way, from the top:
  `git checkout feature-c && git rebase --update-refs main`.
- Once A is submitted and synced, its commits are on `main` as a sync commit with a different history, so replay only
  the branches above it onto `main`. Then clean up A as after any submit, and B is next up for review:
  ```sh
  git checkout feature-c && git rebase --update-refs --onto main feature-a
  git branch -D feature-a && git p4son alias delete feature-a
  git checkout feature-b && git p4son review -F /tmp/b.txt -b main
  ```

## Syncing

```sh
git p4son sync              # sync main to the latest CL, from any branch
git rebase main             # then bring the current branch up to date
```

`sync` commits onto the sync branch, `[sync] branch` in `.git-p4son/config.toml` (usually `main`), and returns to
the branch it started from, unless it merged local changes back: then it stays on the sync branch with them.

- `sync 12345` syncs to a specific CL. Syncing backwards needs `--force`; only do that when the user asks.
- Changelists submitted by the split users each get a sync commit of their own, so they stand apart on `main`
  (`git p4son sync-split-users` lists them). When the user asks to split out someone else's changelists too, pass
  `-u <p4 user>` for that sync, and preview the resulting sync sequence with `--dry-run`. Add them for good with
  `sync-split-users add` only when the user asks for that.
- If the workspace was synced by another tool, run `git p4son sync <that CL>` so git records the same state before
  you branch.
- Every sync first re-syncs to the last synced CL, so Perforce's have revisions match the files git has on disk
  before it syncs forward. When the two disagree, for example after `p4 revert -k` left older content on disk than
  the revision Perforce says you have, a plain `sync` fixes it: don't force-sync or flush files by hand.
  `sync last-synced` runs only that first step.
- A sync can take a long time on a big workspace. Allow for it rather than giving up early.
- If the user has listed blocking processes in `.git-p4son/config.toml` (`[sync] blocking-processes`), `sync`
  refuses to start while one of them, such as the Unreal editor, is running. Ask the user to close it. The error
  mentions `--ignore-blocking-processes`; don't pass it unless the user tells you to.
- Read the summary at the end. Local changes committed on `main` come back as unstaged changes, possibly with
  conflict markers (see the ground rules). Report them to the user rather than resolving them yourself.

## Creating a review

The usual way to put a branch up for review is `review`. It checks out the branch's commits one at a time, oldest
first, creates or updates the CL on each and shelves it, then returns to the branch. Each shelf becomes a patch in
the Swarm review, so every git commit shows up as its own patch and reviewers can step through the commits in order.
`review` is short for `new --review --per-commit`.

1. Write the CL description to a file outside the workspace: the title on the first line, then a short description
   of the change. Everything above `Changes included:` is yours, and `update` keeps it; git-p4son writes the commit
   list below it. `git p4son list-changes -b main --porcelain` prints only that list, numbered as the CL will hold
   it, which helps when summarising the branch.
2. Preview the full description and the files the CL would get, then the steps, one per commit:
   ```sh
   git p4son new -F /tmp/my-feature.txt -b main --dry-run
   git p4son review -F /tmp/my-feature.txt -b main --dry-run
   ```
   The `new` preview lacks the `#review` line the real review ends the description with, and fails if the branch's
   alias already exists.
3. Run it:
   ```sh
   git p4son review -F /tmp/my-feature.txt -b main
   ```
4. Report the CL to the user: `git p4son alias show`. Within seconds Swarm replaces `#review` in the description
   with `#review-<id>`, so `p4 describe -s <CL>` shows the review id too.

Notes:

- `-b` is required: pass `main`, or the branch you started from. The alias records it, with the commits the CL
  includes, so later updates know which commits are new.
- By default the description ends with a numbered list of the commit subjects under `Changes included:`.
  `--no-commit-list` leaves it out, so the description holds exactly the text from the file, plus `#review`. Use it
  for a single-commit change, where a one-entry list would just repeat the title.
- If a step fails, `review` returns to the branch and names the step and commit that failed. Tell the user what
  failed. Once the CL exists, the error gives the `git p4son update ... --per-commit --shelve` command that finishes
  the review after the fix; when the first step (`new`) failed, there is no CL yet, so run `review` again.

To create a CL without the per-commit steps, use `new`:

```sh
git p4son new -F /tmp/my-feature.txt -b main            # CL with all files changed since main
git p4son new -F /tmp/my-feature.txt -b main --review   # same, plus shelve and create a Swarm review
```

## Updating a CL after feedback

Commit the fixes as new commits on top of the branch, without rewriting the reviewed ones, then update the CL once
per new commit, so each one becomes its own patch, as `review` does:

```sh
git p4son update --per-commit --shelve --dry-run   # check the steps
git p4son update --per-commit --shelve
```

The alias of a CL that `new` or `review` made records which commits the CL includes, so `update` works out the
new ones by itself; don't pass `-b`. Each step opens the changed files in the CL, reverts files that are no longer
part of the change, and appends the commit to the commit list; `--shelve` replaces the shelf, which adds a new patch
to the Swarm review. The entries the commit list already has are left as they are, so the user may reword them in
the CL. If a step fails, `update` returns to the branch and names the step that failed: tell the user what failed;
after fixing it, run the same command again, which skips the commits that worked.

- **"... is gone from the branch"** is a warning: a commit the CL includes was dropped or squashed, and its patch
  stays in the review. The update goes on. Tell the user, and next time undo a shelved commit by reverting it in a
  new commit.
- **"... sits below commits CL N already includes"** means a new commit was moved under reviewed ones. Ask the user
  before moving it after them with a rebase.
- **After amending or squashing into an included commit**, such as `jj squash` into a reviewed change, `update` sees
  nothing new there. With no new commits it opens every file since the base anyway; with new ones as well, pass
  `-b main` so the amend's files are opened.
- **After a sync**, `update --shelve` may insert an empty commit, "Rebase onto CL N", after the included commits, so
  the sync's changes get a patch of their own. That's expected: leave it on the branch.
- **A CL whose alias records nothing**, made by `alias new` or an older git-p4son, needs the range: count the new
  commits by comparing the commit list in `p4 describe -s <CL>` with `git log --oneline main..`, then run
  `git p4son update -b HEAD~2 --per-commit --shelve`, replacing 2 with the number.

To reword the description, `-F FILE` or `-m MESSAGE` replaces everything above the commit list. Add `--no-edit` to
change only the description and leave the files alone. `--no-commit-list` leaves the commit list as it is.

## Binary assets in a review

Git-ignored files never travel with a commit, but the user can open them in the CL by hand, for example a string
table that goes with a code change. Two things decide which patch they land in:

- **A placeholder commit gives the asset its own entry.** An empty commit whose subject describes the asset change,
  `git commit --allow-empty -m "Add the Other string to the quests string table"`, puts a line in the commit list.
  `review` and `update --per-commit` handle it like any other commit. Its step adds no patch until the asset is in
  the CL: shelving files that have not changed is a no-op in Swarm.
- **Every shelve takes everything open in the CL.** git-p4son replays git-tracked files commit by commit, but an
  asset already open in the CL goes up with the first shelve of a run, whichever commit it belongs to.

So keep the asset out of the review CL while the commits before it are shelved, for example in the default
changelist. Put the placeholder commit last, run `review` or `update --per-commit --shelve` as usual, then move the
asset into the CL and shelve once more. The asset then arrives as its own patch, right after the code:

```sh
p4 reopen -c $(git p4son alias show) <asset>
git p4son update --shelve
```

Moving the asset is a P4V step for the user if they prefer; it is not a file git-p4son manages.

## After the CL is submitted

Once the user says the CL is submitted:

```sh
git checkout main
git p4son sync
git branch -D my-feature            # -D: the submitted change has a different history than the branch
git p4son alias delete my-feature
```

Ask before deleting a branch or alias if you are not sure the CL was submitted. `git p4son alias clean` deletes
only the aliases whose CL is no longer pending, submitted or deleted, so it is safe to run for leftover aliases.
Run it with `--porcelain`, which prints `<action> <alias> <CL>` per alias (`kept`, `deleted` or `failed`, and with
`--dry-run` `would-keep` or `would-delete`), and with `--dry-run` first to see what would go.

## When something refuses

- **The workspace is not clean.** Commit or stash first; untracked files count. Park something unrelated, like a
  scratch file, with `git stash push -u -- <path>`, and restore it with `git stash pop` once git-p4son is done.
- **`sync` refuses because of p4-opened files** ("Workspace has p4-opened files tracked by git. Revert them
  first: ..."). Files that git
  tracks and that are opened in Perforce would collide with the commit a sync makes. When they are unchanged, for
  example checked out but never edited, revert just those with `p4 revert -a <files>`: `-a` only reverts files
  whose content is unchanged, so edited files stay opened for the user to deal with. Ask the user first. Files in a
  CL made by `new` or `review` are opened too, and usually unchanged while `main` is checked out; reverting them
  keeps the shelf and the Swarm review. `git p4son update -b main` opens them again later; add `--shelve` only when
  the user asks to re-shelve, as a shelve from a narrower base would drop the other commits' files.
- **The alias already exists.** An earlier attempt saved it. `-f` overwrites it, or `git p4son alias delete` removes
  it; ask the user first if the old CL may still be in use.
- **A tracked file turned read-only**, for example after a manual `p4 revert`. With writable mode on,
  `git p4son writable apply` restores the mode for all tracked files.
- **`.git-p4son/config.toml` is invalid** ("... in config.toml must be ..." or "... is not valid TOML"). Report
  the message to the user; don't edit the file yourself.
- **In a jj workspace**, "The jj working copy is busy" means a jj command is running; try again once it finishes.
  "Run jj p4son from inside the workspace; -R is not supported" means `jj -R` named another workspace: `cd` into it
  instead. See "jj workspaces" for the rest.
- **Something else fails.** `git p4son -v <command>` prints every git and p4 command it runs and their output, the
  fastest way to see why.
