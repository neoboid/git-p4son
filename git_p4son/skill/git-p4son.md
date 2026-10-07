# Working with git-p4son

This git repository is also a Perforce workspace. git-p4son keeps the `main` branch in step with the Perforce depot.
Work happens on git feature branches, which git-p4son turns into Perforce changelists (CLs) and Swarm reviews.

Run commands from anywhere inside the workspace as `git p4son <command>`. Use `-h` for help: git swallows `--help`.

## Ground rules

- **The git repository is local only.** It has no remotes: Perforce is where changes are shared. Don't push, pull,
  fetch or open pull requests. Changes reach others as CLs and Swarm reviews, and other people's changes arrive
  through `git p4son sync`.
- **`main` mirrors Perforce.** `git p4son sync` adds a commit to it for each sync. Do your work on a branch off
  `main`. The user may keep small local changes committed on `main`, such as local configuration. When a sync touches
  those files, it merges the local changes on top of the new Perforce content and leaves the result as unstaged
  changes, with conflict markers if they clash. Leave reviewing, resolving and re-committing them to the user.
- **Let git-p4son talk to Perforce.** Don't run `p4 sync`, `p4 edit`, `p4 add`, `p4 delete`, `p4 revert`, `p4 shelve`
  or `p4 change` on files git-p4son manages. Read-only commands such as `p4 describe`, `p4 opened` and `p4 changes`
  are fine.
- **Never submit.** git-p4son has no submit command on purpose: submitting is left to the user, usually in P4V.
- **Reviews and shelves are seen by others.** `review`, `new --review` and `--shelve` publish the change to Swarm
  and its reviewers. Only run them when the user asked for a review or a shelf.
- **Keep the workspace clean.** `sync`, `new` and `update` refuse to run with uncommitted changes, untracked files
  included. Commit or stash first, and keep scratch files, like a description file, outside the workspace.
- **Preview first.** `sync`, `new`, `update` and `review` take `-n/--dry-run`, which prints what would happen without
  touching Perforce or git.
- **Avoid the interactive commands.** `init` and `alias clean` prompt for answers. Setup is the user's job, and
  `alias delete` covers cleaning up.

## Where things stand

- `git log --oneline main` shows the sync history. A sync commit has a subject like
  `git-p4son: p4 sync //workspace/...@12345`, where 12345 is the synced CL.
- `git p4son alias list` lists the aliases: names for the CLs that `new` and `review` created.
- `git p4son alias show [alias]` prints only the CL number of an alias. It defaults to the current branch.
- `p4 describe -s <CL>` and `p4 opened -c <CL>` show a CL's description and files.

## Aliases and the branch name

`new` and `review` save the CL they create under an alias, by default the current branch name with `/` replaced by
`-` (`feature/login` becomes `feature-login`). `update` and `alias show` default to the same alias, so on the branch
that created a CL you rarely need to name it. On a detached HEAD, pass the alias or CL number explicitly.

## Syncing

```sh
git checkout main
git p4son sync              # sync to the latest CL
git checkout my-feature
git rebase main
```

- `sync 12345` syncs to a specific CL. Syncing backwards needs `--force`; only do that when the user asks.
- A sync can take a long time on a big workspace. Allow for it rather than giving up early.
- If the user has listed blocking processes in `.git-p4son/config.toml` (`[sync] blocking-processes`), `sync`
  refuses to start while one of them, such as the Unreal editor, is running. Ask the user to close it. Don't pass
  `--ignore-blocking-processes` unless the user tells you to.
- Read the summary at the end. Local changes committed on `main` come back as unstaged changes, possibly with
  conflict markers (see the ground rules). Report them to the user rather than resolving them yourself.

## Creating a review

The usual way to put a branch up for review is `review`. It replays the branch's commits with an interactive rebase
onto the base branch, and after each commit creates or updates the CL and shelves it. Each shelf becomes a patch in
the Swarm review, so every git commit shows up as its own patch and reviewers can step through the commits in order.

1. Write the CL description to a file outside the workspace. `git p4son list-changes -b main` prints the commit
   subjects, which helps when summarising the branch.
2. Check the generated rebase todo:
   ```sh
   git p4son review -F /tmp/my-feature.txt -b main --dry-run
   ```
3. Run it, accepting the todo without an editor:
   ```sh
   git p4son review -F /tmp/my-feature.txt -b main --no-edit-todo
   ```
4. Report the CL to the user: `git p4son alias show`.

Notes:

- Always pass `-b main` (or the branch you started from). The default base is `HEAD~1`, which only covers the last
  commit.
- Keep the description file until the rebase has finished: each step reads it.
- By default the description ends with a numbered list of the commit subjects under `Changes included:`.
  `--no-commit-list` leaves it out, so the description holds exactly the text from the file.
- If the alias already exists, `review` stops. Use `-f` to overwrite it only when the old CL is no longer needed.
- If a step fails, the rebase stops. Tell the user what failed; after fixing it, `git rebase --continue` carries on.

To create a CL without the per-commit steps, use `new`:

```sh
git p4son new -F /tmp/my-feature.txt -b main            # CL with all files changed since main
git p4son new -F /tmp/my-feature.txt -b main --review   # same, plus shelve and create a Swarm review
```

## Updating a CL after feedback

Commit the changes on the branch, then add the new commit to the CL and re-shelve:

```sh
git p4son update --shelve
```

`update` opens the changed files in the CL, reverts files that are no longer part of the change, and adds the commit
to the commit list. `--shelve` replaces the shelf, which adds a new patch to the Swarm review.

With several new commits since the last update, run `update` once per commit so each one becomes its own patch, as
`review` does. A rebase with `--exec` does that without an editor; replace 2 with the number of new commits:

```sh
git rebase HEAD~2 --exec "git p4son update --shelve --sleep 5"
```

To find the number, compare the commit list in `p4 describe -s $(git p4son alias show)` with
`git log --oneline main..`. The sleep gives Swarm time to process each shelf before the next one.

To reword the description, `-F FILE` or `-m MESSAGE` replaces everything above the commit list. Add `--no-edit` to
change only the description and leave the files alone. `--no-commit-list` leaves the commit list as it is.

## After the CL is submitted

Once the user says the CL is submitted:

```sh
git checkout main
git p4son sync
git branch -D my-feature            # -D: the submitted change has a different history than the branch
git p4son alias delete my-feature
```

Ask before deleting a branch or alias if you are not sure the CL was submitted.
