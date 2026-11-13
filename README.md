# Git Perforcesson

`git-p4son` lets you work in a Perforce workspace with a local git repository, or a colocated
[Jujutsu (jj)](#jujutsu-jj) one, so you can use the familiar local development flow even though your project uses
Perforce.

The core idea is a local `main` branch kept in sync with the Perforce workspace. Each sync commits the changes since
the last one. You work on feature branches based on `main`, and rebase them onto it after a sync.

When your feature is ready, `git-p4son` creates a changelist from the commits on your branch. It can also create a
review, in Swarm or another review tool, with each commit as its own patch, so reviewers see the changes in the order
you made them, like the commits of a pull request on GitHub. To address feedback, commit more locally, and
`git-p4son` adds the new commits to the changelist and the review.

## Usage Example

Here's a typical workflow when developing a feature using `git-p4son`:

```sh
# Sync main with the latest changes from Perforce
git checkout main
git p4son sync

# Start work on a new feature
git checkout -b my-fancy-feature

# Change some code
git add .
git commit -m "Feature part1"

# Sync again, and rebase your branch onto main
git checkout main
git p4son sync
git checkout my-fancy-feature
git rebase main

# Change even more code
git add .
git commit -m "Feature part2"

# Create a review with a patch per commit, under an alias named after the branch
git p4son review -m "My fancy feature" -b main

# Address review feedback, then add the new commit to the changelist and the review
git add .
git commit -m "Address review feedback"
git p4son update --shelve

# After approval, submit in P4V, then sync and clean up
git checkout main
git p4son sync
git branch -D my-fancy-feature    # -D, as the submitted change has other commit hashes
git p4son alias clean             # deletes the aliases of submitted changelists

# Start working on the next feature
git checkout -b my-next-fancy-feature
```

### With jj

The same workflow in a [colocated jj workspace](#jujutsu-jj), with the [`jj p4son` alias](#the-jj-p4son-alias):

```sh
# Start work on a new feature
jj new main -m "Feature part1"
# Change some code, which jj snapshots into the change

# Sync main from any change: the change you are on is rebased onto it
jj p4son sync

# Change even more code in a change of its own
jj new -m "Feature part2"
jj bookmark create my-fancy-feature

# Create a review with a patch per change, under an alias named after the bookmark
jj p4son review -m "My fancy feature" -b main

# After review feedback, add a change and move the bookmark to it
jj new -m "Address review feedback"
jj bookmark set my-fancy-feature -r @
jj p4son update --shelve

# After approval, submit in P4V, abandon the submitted changes, which deletes the
# bookmark too, then sync and clean up
jj abandon main..my-fancy-feature
jj p4son sync
jj p4son alias clean

# Start working on the next feature
jj new main -m "Next fancy feature"
```

`sync` only rebases the change you sync from, and the stack above it. Bring other bookmarks up to date with
`jj rebase -b <bookmark> -d main`.

## Contents

- [Installation](#installation)
- [Setup](#setup)
- [Commands](#commands): [init](#init-command), [sync](#sync-command), [cherry-pick](#cherry-pick-command),
  [sync-split-users](#sync-split-users-command), [new](#new-command), [update](#update-command),
  [review](#review-command), [list-changes](#list-changes-command), [alias](#alias-command),
  [review-tool](#review-tool-command), [writable](#writable-command), [completion](#completion-command),
  [skill](#skill-command)
- [Configuration](#configuration)
- [Review Tools](#review-tools)
- [Hooks](#hooks)
- [Jujutsu (jj)](#jujutsu-jj)
- [Claude Code Skill](#claude-code-skill)
- [Shell Completions](#shell-completions)
- [Development](#development)

## Installation

```sh
pip install git-p4son
```

Requires Python 3.11 or later. For tab completion in bash, zsh and PowerShell, see
[Shell Completions](#shell-completions). To let Claude Code drive git-p4son, see
[Claude Code Skill](#claude-code-skill).

### Updating

```sh
pip install --upgrade git-p4son
```

## Setup

These steps set up git-p4son in an existing Perforce workspace. You only need to do this once.

1. **Sync your workspace to a known changelist.** Pick a changelist to use as the starting point for your git
   history:
   ```sh
   p4 sync //...@12345
   ```

2. **Run `git p4son init`.** This can be anywhere inside your Perforce workspace - it doesn't have to be at the
   root:
   ```sh
   cd /path/to/your/workspace    # or a subdirectory of it
   git p4son init
   ```
   Answer its questions, starting with the depot root: the whole workspace or the subtree you're in. It then runs
   `git init`, sets up `.gitignore` and makes a first commit holding only that. See [Init Command](#init-command) for
   what it asks.

3. **Review `.gitignore`.** Edit the file to ensure build artifacts and other unwanted files are excluded. In a big
   workspace, such as an Unreal Engine game with all of the engine source in it, git can struggle to track
   everything. Ignore all but the directories you work in; Perforce still manages the rest.

4. **Add and commit all files manually the first time**
   ```sh
   git add .
   git commit -m "Initial submit all files"
   ```

5. **Run `git p4son sync`** to get your first official sync commit
   ```sh
   git p4son sync
   ```
   This creates a commit with message that records the CL you have synced.

From here, branch off `main` for local development. See the [Usage Example](#usage-example) for a typical
workflow.

## Commands

git-p4son provides thirteen commands:

| Command | What it does |
| --- | --- |
| [`init`](#init-command) | Set up a git repository in a Perforce workspace |
| [`sync`](#sync-command) | Sync the workspace from Perforce and commit the changes to `main` |
| [`cherry-pick`](#cherry-pick-command) | Sync only a submitted changelist's files and commit them to `main` |
| [`sync-split-users`](#sync-split-users-command) | Show or edit the users whose changelists `sync` commits one by one |
| [`new`](#new-command) | Create a changelist from the commits on your branch, optionally with a review |
| [`update`](#update-command) | Add the newer commits to an existing changelist |
| [`review`](#review-command) | Create a review with one shelf per commit |
| [`list-changes`](#list-changes-command) | List the commit subjects since a base branch |
| [`alias`](#alias-command) | Manage the names that stand for changelist numbers |
| [`review-tool`](#review-tool-command) | Show or set the default review tool, and the one an alias records |
| [`writable`](#writable-command) | Keep git-tracked files writable, without `p4 edit` first |
| [`completion`](#completion-command) | Print the path to a shell completion script |
| [`skill`](#skill-command) | Install or show the Claude Code skill |

To see help for any command, use `-h`:

```sh
git p4son -h
git p4son sync -h
```

**Note:** When invoking via `git p4son`, the `--help` flag is intercepted by git (to look for man pages). Use `-h`
instead, or `git p4son -- --help` to force it through.
Alternatively, call the executable directly: `git-p4son --help`.

**Global options:**
- `-v, --verbose`: Show verbose output (commands, elapsed times, raw subprocess output)
- `--version`: Show program version

### Init Command

Set up git-p4son in a Perforce workspace, from anywhere inside it:

```sh
git p4son init
```

`init` checks that it runs in a Perforce workspace, then asks:

- **Depot root:** which part of the workspace git-p4son syncs, the whole workspace or the subtree the git root is in.
  It's saved as `[depot] root` in `.git-p4son/config.toml`, with a `$(workspace)` placeholder for the workspace name,
  e.g. `//$(workspace)/Engine`, so renaming the workspace doesn't break it. A concrete name works too.
- **Writable mode:** whether to turn on [writable mode](#writable-command), defaulting to the current setting. It only
  saves the answer; run `git p4son writable apply` once your files are committed.
- **Split users:** whether to sync your own changelists one by one, as a [split user](#split-users), defaulting to the
  current setting. Yes adds a `$(user)` entry and no removes it; other split users are left alone.
- **Claude Code skill:** if Claude Code is installed, whether to install the [skill](#claude-code-skill). An installed
  skill is refreshed without asking.
- **jj:** with jj 0.41 or newer, whether to colocate jj and add the `jj p4son` alias, as described below.

Then it sets up the repository:

- **`.gitignore`:** kept if it exists, else copied from `.p4ignore`, else created empty.
- **git:** `git init`, and a first commit holding only `.gitignore`.
- **Sync branch:** recorded as `[sync] branch`. In a repository it creates, that's the branch `HEAD` is on. In an
  existing one, it's `main` if there is one, else the branch `HEAD` is on, else one you pick from the local branches.
  A branch already set is kept.

It ends by listing the next steps. Nothing in `.git-p4son/` belongs in git: the folder has its own `.gitignore` that
ignores everything in it.

For [jj](#jujutsu-jj), `init` offers:

- **Colocating jj**, when the workspace has no `.jj` folder: "Use jj in this workspace too (colocated)?", default
  no. On yes, in an existing repository it runs `jj git init --colocate` as its last step, so jj imports the branch
  as a bookmark. It sets jj's [line endings](#line-endings) to match git's from jj's first snapshot on. In a
  repository it creates, it lists the same commands as next steps after your initial commit instead, since jj would
  otherwise snapshot every file before you have reviewed `.gitignore`.
- **The [`jj p4son` alias](#the-jj-p4son-alias)**, when the workspace has a `.jj` folder, or is to get one, and
  jj's config has no `aliases.p4son`: default yes. It adds the alias to jj's user config.

Running `init` again asks neither once they are done.

### Sync Command

Sync the workspace from Perforce and commit what changed onto the sync branch, `main` by default:

```sh
git p4son sync [changelist ...] [--branch BRANCH] [--no-rebase] [--allow-backwards] [--dry-run]
               [--split-user NAME ...] [--no-split] [--ignore-blocking-processes]
```

**Arguments:**
- `changelist` (optional): Changelist number, `last-synced` to re-sync the last synced changelist, or `head`
  to sync to the latest changelist. Omit to sync to the latest changelist affecting the workspace. Several
  strictly increasing changelist numbers may be given to sync each in sequence, producing one commit per
  changelist. A trailing `head` may close out such a sequence to finish at the latest changelist.

**Options:**
- `--branch BRANCH`: Commit onto this branch instead of `[sync] branch`. With the jj backend, the bookmark to move
  to each sync commit
- `--no-rebase`: With the jj backend, leave the changes you started from where they are, instead of rebasing them
  onto the [sync bookmark](#syncing-a-bookmark)
- `--allow-backwards`: Allow syncing to changelists older than the last synced one. `-f, --force` still works for
  now, with a warning
- `-n, --dry-run`: Print the resolved sync sequence without syncing. The arguments are validated as for a
  real sync, but nothing else runs: no clean-workspace checks, no hooks, and no prompts. Ends with
  `Dry run, nothing changed`
- `-u, --split-user NAME`: Also split out this user's changelists, on top of the configured
  [split users](#split-users). Repeat to give several users. Each name is checked against the Perforce server.
- `--no-split`: Ignore the configured split users for this sync. Users given with `--split-user` are still split
  out, so `--no-split -u alice` splits out only alice's changelists.
- `--ignore-blocking-processes`: Sync even while a [blocking process](#blocking-processes) is running.

**Examples:**
```sh
git p4son sync              # sync to latest
git p4son sync head         # sync to latest (explicit)
git p4son sync 12345
git p4son sync 123 156 178  # sync each changelist in sequence, one commit each
git p4son sync 123 156 head # sync 123, 156, then the latest changelist
git p4son sync last-synced
git p4son sync 12345 --allow-backwards
git p4son sync --dry-run    # show the changelists a sync to latest would visit
git p4son sync -u alice     # also split out alice's changelists
git p4son sync --no-split   # sync without splitting out the configured split users
git p4son sync --branch p4  # commit onto the p4 branch this time
```

`sync` commits onto the sync branch, `[sync] branch` in `.git-p4son/config.toml`, which `init` sets and which
defaults to `main`. From another branch, it checks the sync branch out, syncs and commits, then returns you to
where you started, telling you to `git rebase` your branch onto the sync branch; it doesn't rebase for you. The
pre-sync and post-sync hooks run on the sync branch. You stay on the sync branch when:

- local changes were [merged back](#how-sync-handles-writable-files) into the work tree, for you to review and
  commit there
- a sync fails partway, with the files as Perforce left them and the commits so far kept

A failing pre-sync hook, or anything else that stops `sync` before it syncs, returns you to where you started.

`sync` requires clean git and Perforce workspaces: no uncommitted changes, untracked files included, and no
git-tracked files opened in Perforce. It checks them after any [blocking processes](#blocking-processes) and before
the [pre-sync hooks](#pre-sync-hook).

#### Blocking processes

Syncing while e.g. the Unreal editor is open lets p4 replace assets the editor still has loaded, so the editor
keeps working against files that no longer match what is on disk. List such processes in `.git-p4son/config.toml`
and `sync` refuses to start while any of them is running:

```toml
[sync]
blocking-processes = ["UnrealEditor", "UnrealLightmass"]
```

Names are matched with any `.exe` suffix stripped and case ignored, so a single entry covers `UnrealEditor.exe` on
Windows and `UnrealEditor` on macOS. The check runs before the clean-workspace checks and the pre-sync hooks. A
[malformed setting](#configuration), or a process list that cannot be read, aborts the sync rather than silently
skipping the check.

Pass `--ignore-blocking-processes` to sync once without the check, or remove the setting to turn it off for good.

#### Split users

Syncing straight to the latest changelist lumps your own submitted changelists together with everyone else's in a
single commit. Split users fix that: for each changelist a split user submitted since the last sync, `sync` first
syncs the changelist submitted immediately before it, so every changelist they submitted becomes a commit containing
nothing but their own change.

The split users are stored in `.git-p4son/config.toml`, and edited with the
[`sync-split-users` command](#sync-split-users-command) or by answering yes in `init`:

```toml
[sync]
split-users = ["$(user)", "alice"]
```

`$(user)` stands for the current Perforce user (from `p4 info`), resolved each time `sync` runs, so the config
survives a change of user, the same way `$(workspace)` works in the depot root.

For example, with the last sync at CL 100, your own changelists at 102 and 105, and the latest changelist at 106,
`sync` visits `101 102 104 105 106`, producing five commits where CL 102 and CL 105 each hold only your change.
Changelists given on the command line are all still visited, with the split points merged in: `sync 103 head`
visits `101 102 103 104 105 106`.

Splitting only moves forward from a previous sync. The first sync, `last-synced`, and syncing to an older changelist
with `--allow-backwards` are never split. Without any split users, `sync` makes no extra Perforce queries.

Finding the split users' changelists takes one Perforce query per split user and one more, none of which scans every
file in the range, so a large range stays within the server's `maxscanrows`. They run after the checks and the
pre-sync hooks, so those fail first. `--dry-run` skips the checks and hooks but still shows the resulting sequence.

#### Sync hooks

`sync` runs the [pre-sync](#pre-sync-hook) and [post-sync](#post-sync-hook) hooks.

#### How sync handles writable files

When you switch branches with `git checkout`, git removes the read-only flag on every file it writes. This means
that when you switch back to `main` for a sync, Perforce may refuse to overwrite those files because they are
writable. git-p4son handles these automatically by classifying each writable file:

**writable-unchanged** - The file on disk is identical to the last sync (only the read-only flag was removed
by git). This is the common case after switching branches. git-p4son detects this by comparing git blob IDs against
the last sync or [cherry-pick](#cherry-pick-command) commit that touched the file, restores the read-only flag, and
lets the sync overwrite the file.

**writable-changed** - The file on disk differs from the last sync. This happens when you manually edit a file
on `main` and commit it to git without submitting to Perforce. git-p4son lets the sync overwrite the file and
includes it in the sync commit (so the commit reflects pure Perforce state), then three-way merges your local
changes on top of the new Perforce content - similar to a `git rebase`. If the merge is clean, the result appears
as an unstaged change. If there are conflicts, standard conflict markers are inserted for you to resolve.

**added both locally and in Perforce** - A file you committed to git is also being added by Perforce (p4 has never
synced it to your workspace). There is no common baseline, so git-p4son merges the two versions against an empty
base: identical content merges cleanly, otherwise the file gets a single conflict block containing both full
versions for you to resolve.

**binary files** - Binary files that have local changes cannot be three-way merged. git-p4son restores your local
version to disk after the sync commit so it shows up in `git status` for you to handle manually.

**added locally, deleted upstream** - A file with local changes in git was deleted in Perforce. git-p4son lets the
delete stand and warns you - your version remains available in git history.

**deleted locally, added upstream** - A file you deleted from git was modified in Perforce. The Perforce version is
kept in the sync commit. You can delete it again if you want.

**ignored** - Files ignored by `.gitignore` are skipped with a warning. These are outside git's domain and should
be managed manually or via Perforce directly.

After syncing, git-p4son prints a summary of how each writable file was handled, and instructs you to review and
commit any files that need attention.

**Workspaces with `allwrite`** - When your client spec has the `allwrite` option, every file is writable by design,
so the read-only flag carries no information about local changes. git-p4son classifies files exactly as above (the
verdict comes from git, not from the flag), but leaves the write bit alone on files it finds unchanged: Perforce
compares digests before refusing to overwrite a writable file, and unchanged files pass that check. Only files with
local changes are made read-only for the duration of the sync, since those are the ones Perforce would otherwise
refuse to overwrite. The same digest check applies to git-ignored files: unchanged ones sync normally, and after the
sync git-p4son lists the ones Perforce skipped because they were modified locally.

**Writable mode** - With [writable mode](#writable-command) on, the files a sync touches are made writable again after
the sync if git tracks them. The sync itself works as described above, since the workspace is not `allwrite`.

### Cherry-Pick Command

Sync only the files of one or more submitted changelists, at the revisions they submitted, and commit them onto the
sync branch, one commit per changelist. It is `p4 sync //workspace/...@=CHANGELIST`, for when you need a fix that
was just submitted without syncing everything else:

```sh
git p4son cherry-pick changelist [changelist ...] [--no-rebase] [--dry-run] [--ignore-blocking-processes]
```

**Arguments:**
- `changelist`: Submitted changelist numbers, cherry-picked in the order given, each once. Each must come after the
  last synced changelist.

**Options:**
- `--no-rebase`, `-n, --dry-run` and `--ignore-blocking-processes`: as for [`sync`](#sync-command). A dry run checks
  the changelists and prints them.

**Examples:**
```sh
git p4son cherry-pick 12345
git p4son cherry-pick 12345 12352   # one commit each
```

Like `sync`, it runs from any branch, onto the sync branch only, with the same clean-workspace checks, hooks,
handling of [writable files](#how-sync-handles-writable-files), and return to where you started, or with jj the
rebase of your changes onto the [sync bookmark](#syncing-a-bookmark). Perforce's have list is one for the whole
workspace, so the sync branch is the one branch it matches; rebase your branches onto it as after a sync.

- **The commit is the p4 command:** `git-p4son: p4 sync //workspace/...@=12345`, next to a sync's `...@12345`.
  Sync's "last synced" changelist stays the one before the cherry-pick.
- **Whole files:** each file comes at its revision from that changelist, with any earlier changelists' edits to the
  same file, but not those changelists' other files, as `p4 sync @=` does.
- **Not before the last sync:** a changelist at or before the last synced one is refused, as it would take files
  back to older revisions. There is no `--allow-backwards`.
- **In any order after it:** a changelist found to be needed after a newer one was cherry-picked can still be
  picked. A file both changed goes to the revision of the one picked last, with a warning that names them.
- **Submitted only:** a pending or shelved changelist is refused; unshelve one with p4 instead.
- **The next sync** takes the files to the revisions it syncs to: past the cherry-picked changelist they stay, and
  before it they go back, which it warns about. With [split users](#split-users), a split commit below a
  cherry-picked changelist shows its files go back, and the next commit brings them forward again.
  `sync last-synced` re-syncs the cherry-picks too.
- **`update --shelve`'s catch-up commit** names the cherry-picks too, such as "Rebase onto CL 12340 with
  cherry-picks 12345, 12352".

### Sync-Split-Users Command

Show or edit the [split users](#split-users), the Perforce users whose changelists `sync` gives a commit each:

```sh
git p4son sync-split-users                 # list the split users (same as list)
git p4son sync-split-users list
git p4son sync-split-users add --me        # add yourself, stored as $(user)
git p4son sync-split-users add alice bob
git p4son sync-split-users delete alice
git p4son sync-split-users delete --me
```

- `list` prints one user per line. `$(user)` is shown with the name it stands for, e.g. `$(user) (alice)`.
- `add` checks every name against the Perforce server first, since a misspelled name would never match a
  changelist. If any name is not a Perforce user, nothing is added. Names are stored as the server spells them,
  and a name that is already a split user is skipped.
- `delete` removes names regardless of case. If any name is not a split user, nothing is removed.

`--me` adds or removes the `$(user)` entry. A quoted `'$(user)'` does the same, but the quotes matter: unquoted,
bash, zsh and PowerShell run `$(user)` as a command substitution.

### New Command

Create a changelist from the commits since the base branch: their files are opened in it, and its description lists
the commits. Optionally shelve it, or create a review in Swarm or another [review tool](#review-tools):

```sh
git p4son new [-b BASE_BRANCH] [-m <message> | -F <file>] [alias] [--force] [--dry-run] [--no-edit]
              [--no-alias] [--no-commit-list] [--shelve] [--review] [--review-tool NAME] [--per-commit]
              [--ignore-out-of-date]
```

**Arguments:**
- `alias` (optional): Alias name to save the new changelist number under. Defaults to the current branch name.

**Options:**
- `-m, --message MESSAGE`: Changelist description message. Defaults to the HEAD commit subject
- `-F, --file FILE`: Read the changelist description message from a file instead of `-m`. The message may span
  multiple lines
- `--no-commit-list`: Leave the enumerated commit list out of the changelist description, so it holds only the
  message
- `-b, --base-branch BASE_BRANCH`: The base the change starts from, for enumerating commits and finding changed
  files: a branch or bookmark, or a commit exactly one points at, or with jj a change without a bookmark. The alias
  records it by name, or a jj change by its change ID, as a commit would go stale after a sync and rebase, along with
  the commits since it the changelist includes, so `update` knows where the next ones start. With `--no-alias` any
  commit will do. Left out, it defaults to the [branch's local upstream or the bookmark below](#the-default-base)
- `-f, --force`: Overwrite an existing alias file
- `-n, --dry-run`: Pretend and print what would be done, but do not execute. Ends with `Dry run, nothing changed`
- `--no-edit`: Skip opening changed files for edit in Perforce
- `--no-alias`: Skip saving a changelist alias
- `--shelve`: Shelve the changelist after creating it
- `--review`: Shelve and create a review with the default [review tool](#review-tools), Swarm unless
  `[review-tool] default` names another. For Swarm, adds the `#review` keyword
- `--review-tool NAME`: Push the review to this review tool instead of the default one: `swarm`, or a
  `[review-tool.NAME]` in `.git-p4son/config.toml`. Implies `--review`. The alias records the tool, Swarm included,
  which `update --shelve` then runs
- `--per-commit`: Create the changelist on the first commit since the base branch, then update it once per later
  commit, as described below. Refuses `--no-edit` and `--no-alias`
- `--ignore-out-of-date`: Shelve even if files in the change are not at the latest revision (see
  [Shelving only files at the latest revision](#shelving-only-files-at-the-latest-revision))

#### The default base

Without `-b`, `new` and `review` take the base from where the change sits, and the `Resolving base` step says which:

- **git:** the current branch's upstream, when that is a local branch: `git switch -c my-fix --track main`, or
  `git branch -u main` on an existing branch. In a stack, track the branch below. An upstream on a remote, such as
  `origin/main`, doesn't count. Other than the sync branch, the upstream must be below the branch: once it was amended
  or rebased, or the branch was rebased onto `main` past it, such as after the branch below was submitted, it is
  refused, as the commits since it would include others.
- **jj:** the nearest bookmark below the change, such as `main`, or the bookmark below in a stack.

Other than the sync branch itself, a base in its history, such as one left on an old sync commit, is refused too, as
it would take in the sync commits since it. So is a git upstream below a merge of the sync branch. Rebase onto the
base, or with git move the upstream with `git branch -u main`.

Without one, or with two bookmarks on the nearest commit below, `-b` is required. `branch.autoSetupMerge = always`
sets the upstream on every new branch, but with `push.default = upstream` a bare `git push` then pushes the branch
into its local upstream, such as `main`.

**Examples:**
```sh
git p4son new -m "Add feature" -b main
git p4son new -m "Fix bug" myalias -b main
git p4son new -m "Fix bug" --no-alias -b HEAD~1
git p4son new -m "New feature" --review -b main
git p4son new -m "New feature" --shelve --per-commit -b main   # a shelf per commit, without a Swarm review
```

Opening files requires a clean git workspace, untracked files included; `new` refuses to run otherwise. The check
is skipped with `--no-edit`. After opening files, unchanged ones are reverted (see
[Keeping the changelist in sync with git](#keeping-the-changelist-in-sync-with-git)).

With `--per-commit`, `new` checks out the first commit since the base branch and runs `git p4son new` on it, then
checks out each later commit and runs `git p4son update` on it, and finally returns to where you started. With
`--shelve` or `--review`, every step shelves, so each commit shows up as its own patch in the review. With Swarm,
every step but the last sleeps 5 seconds to give Perforce/Swarm time to process the shelf; another review tool runs
once per step instead. It needs a clean git workspace even with `--dry-run`. [`review`](#review-command) is
`new --review --per-commit`, and describes the steps, the dry run and resuming after a failed step.

### Update Command

Add the commits a changelist doesn't include yet: `update` opens their files for edit and appends their subjects to
the commit list in the description, keeping the message above it unless `-m` or `-F` replaces it. It also reverts
files that are no longer part of the change (see
[Keeping the changelist in sync with git](#keeping-the-changelist-in-sync-with-git)). Opening files needs a clean git
workspace, untracked files included, unless `--no-edit` skips it. Before changing anything, `update` refuses a
changelist that isn't pending, yours and on this workspace's client, as `alias new` does.

```sh
git p4son update [changelist] [-m <message> | -F <file>] [--base-branch BASE_BRANCH] [--dry-run]
                 [--no-commit-list] [--no-edit] [--shelve] [--per-commit] [--ignore-out-of-date]
```

**Arguments:**
- `changelist` (optional): Changelist number or named alias to update. Defaults to the current branch name.

**Options:**
- `-m, --message MESSAGE`: Replace the changelist description message above the commit list. The commit list is
  kept and updated as usual, and so is a Swarm `#review` keyword on a line of its own at the end of the description
- `-F, --file FILE`: Like `-m`, but read the message from a file. The message may span multiple lines
- `-b, --base-branch BASE_BRANCH`: For a changelist without a record, where the commits to fold in and the files to
  open start, `HEAD~1` by default. With a record, `update` opens every file since the base it records, so it takes
  `-b` only when that base no longer resolves, as a deleted branch or an abandoned or divergent jj change. `update`
  refuses without `-b` then, and with it records the new base, as `new` does. That base has to sit below the
  changelist's first commit
- `-n, --dry-run`: Pretend and print what would be done, but do not execute. Ends with `Dry run, nothing changed`
- `--no-commit-list`: Leave the enumerated commit list as it is. Without `-m` or `-F` the description is not
  touched at all
- `--no-edit`: Skip opening changed files for edit in Perforce. With a record, the new commits are then neither
  appended nor counted, so the next update that opens files includes them
- `--shelve`: Re-shelve the changelist after updating. For a review in a [review tool](#review-tools) other than
  Swarm, as the alias records it, the tool then runs for the new shelf
- `--per-commit`: Update the changelist once per commit it doesn't include yet, or since the base branch without
  a record, oldest first, with each commit checked out in turn. With `--shelve`, steps sleep 5 seconds between them
  to give Perforce/Swarm time to process each shelf, unless another review tool has the changelist. `--dry-run`
  prints the steps, one per commit
- `--ignore-out-of-date`: Shelve even if files in the change or the changelist are not at the latest revision (see
  [Shelving only files at the latest revision](#shelving-only-files-at-the-latest-revision))

**Examples:**
```sh
git p4son update              # fold the new commits into the changelist for the current branch
git p4son update --shelve     # update and re-shelve
git p4son update 12345
git p4son update -F description.txt   # replace the message, e.g. after review feedback
git p4son update --per-commit --shelve   # one shelf for each commit the changelist doesn't include yet
git p4son update 12345 -b HEAD~2 --per-commit --shelve   # no record: the last two commits
```

#### Which commits are new

A changelist made by `new` or `review` has an alias that records its base and the commits it includes, by jj change
ID or, with git, by author date, which survive rebases and amends. `update` adds only the commits it doesn't know,
whether it shelves or not, and leaves the entries already in the commit list as they are, so an entry you reworded
stays as you wrote it.

- `update` opens every file of the change since the base, so a commit that undoes an earlier one, such as a file
  deleted and then restored, is opened against the depot, and so are files a manual `p4 revert` took out. A file of
  the change that you moved to another changelist is moved back. So `-b` is refused while the recorded base
  resolves: a diff from a commit the changelist already includes would replay its changes against what p4 has opened.
- An included commit that is gone from the branch, dropped or squashed, gets a warning. Its patch stays in the
  review: to undo a shelved commit, revert it in a new commit.
- A new commit below included ones is refused, as its patch would undo the ones above it. Move it after them.
- An amend keeps the commit's identity, so `update` sees it as included, and opening every file since the base takes
  in what it changed.

#### After a sync

A shelf holds whole files, so after a sync that changed files the changelist touches, the next shelf would mix the
sync's changes into your next patch. So when `update --shelve` finds you synced and rebased, it first inserts an empty
commit, `Rebase onto CL <N>`, after the last included commit, and shelves it on its own: its patch holds what the sync
changed. After a [cherry-pick](#cherry-pick-command) it names those too: `Rebase onto CL <N> with cherry-picks <X>,
<Y>`. An update without `--shelve` leaves this to the next one that shelves, and a dry run says what it would insert.

#### Changelists without a record

For a changelist whose alias records no commits, made by `alias new` or an older git-p4son, or one named by a number
no alias holds, `update` takes the commits since `-b`, `HEAD~1` by default. Their entries replace the ones in the
commit list with the same subject, the others are appended, and the list is renumbered.

#### One update per commit

With `--per-commit`, `update` checks out each new commit in turn, oldest first, updates the changelist on it, and
returns to where you started. With `--shelve`, each commit gets a shelf of its own, so it shows up as its own patch in
the review. Without a record, give the range with `-b`, such as `-b HEAD~2` for the last two commits; with one, `-b`
is refused, as it would shelve included commits again. `-m`/`-F` applies to the first step only, and the run needs a
clean git workspace even with `--no-edit`.

If a step fails, `update` returns to where you started and names the commit that failed, with the command that
resumes after the last commit that worked. With a record, running the same command again does the same.

### Keeping the changelist in sync with git

`new` and `update` open every file changed since the base branch in the changelist. A file git sees as renamed is
opened as a `p4 move` from its old path to its new one, carrying any edit along, so Perforce keeps its history and the
review shows the move with only the lines that changed. git finds a rename as `git diff -M` does, by at least half
the content matching, whatever `diff.renames` says. A file renamed again later moves on from where it started, and an
existing move stays opened through later edits. A file added earlier in the same change is renamed as an add, as it
isn't in the depot yet, and a rename that only changes the case of the name is an edit, as a case-insensitive server
sees one file.

A file can later stop being
part of the change, for example when a later commit undoes an edit, or a file is added in one commit and deleted
in the next. Such files would otherwise stay opened as no-ops, so after opening files both commands revert them:

- Files opened for edit that are unchanged compared to the depot are reverted with `p4 revert -a`. Changed files
  stay opened.
- Files opened for add that are missing from disk are reverted.
- Files opened for delete that git still has (deleted in one commit, restored in a later one) are reverted, keeping
  git's file.
- Moves whose new path git no longer has, as the rename was undone, are reverted, which reverts both ends.

Only files git-p4son has authority over are touched: files tracked by git, and files opened for add that are
missing from disk. Anything else in the changelist, such as binaries that only exist in Perforce and were opened
by hand, is left alone even when unchanged. Files opened with other actions (`integrate`, `branch`, ...) are also
left alone. This step is skipped with `--no-edit`.

Shelving (`--shelve`, `--review`) replaces the whole shelf with the changelist's opened files using
`p4 shelve -r -a leaveunchanged`, so the shelf never holds stale files, and unchanged files that are still opened
are left out of it.

#### Shelving only files at the latest revision

Before shelving, `new`, `update` and `review` check that the files are synced to the latest revision in the depot:
the change's files, and for `update` also the files already opened in the changelist, or with `--no-edit` only
those. A file is out of date when someone has submitted a newer revision, deleted it, or added a file of that name
since your last sync. Shelved, it would show in the review against an older revision, and the changelist would need a
resolve before it is submitted, so the command lists the files and refuses, before it changes anything. Run
`git p4son sync`, rebase the change onto it and run the command again, or pass `--ignore-out-of-date` to shelve
anyway; the command it gives to resume or rerun after a failure keeps the option. With `--per-commit`, the check
covers every commit once, before the first step. Without shelving there is no check.

### Review Command

Create a review with one shelf per commit, so each commit is a patch of its own. `review` is short for
`git p4son new --review --per-commit`:

```sh
git p4son review [-b BASE_BRANCH] [alias] [-m <message> | -F <file>] [--force] [--dry-run] [--no-commit-list]
                 [--review-tool NAME] [--ignore-out-of-date]
```

**Arguments:**
- `alias` (optional): Alias name for the new changelist. Defaults to the current branch name.

**Options:**
- `-m, --message MESSAGE`: Changelist description message. Defaults to the HEAD commit subject
- `-F, --file FILE`: Read the changelist description message from a file instead of `-m`. The message may span
  multiple lines
- `-b, --base-branch BASE_BRANCH`: The base the change starts from, to find commits since: a branch or bookmark, or
  a commit exactly one points at, which the alias records as for [`new`](#new-command). Left out, it defaults to the
  [branch's local upstream or the bookmark below](#the-default-base)
- `-f, --force`: Overwrite an existing alias file
- `-n, --dry-run`: Print the steps, one per commit, without running them. Ends with `Dry run, nothing changed`
- `--no-commit-list`: Leave the enumerated commit list out of the changelist description. Passed on to `new` and
  each `update` step
- `--ignore-out-of-date`: Shelve even if files in the change are not at the latest revision (see
  [Shelving only files at the latest revision](#shelving-only-files-at-the-latest-revision))

**Examples:**
```sh
# Review all commits since main (alias defaults to branch name)
git p4son review -m "Add my feature" -b main

# Review a branch's only commit: HEAD~1 is main's commit, so the alias records main
git p4son review -m "Fix bug" -b HEAD~1

# Preview the steps without running them
git p4son review -m "Add my feature" -b main --dry-run

# Take a multi-line description from a file
git p4son review -F description.txt -b main
```

`review` checks out the first commit since the base branch and runs `git p4son new --review` on it, then checks out
each later commit and runs `git p4son update --shelve`, and finally returns to where you started. With Swarm, every
step but the last sleeps 5 seconds to give Perforce/Swarm time to process the shelf; another
[review tool](#review-tools) runs once per step instead. The commits are reviewed where they are: to review them on top
of the latest `main`, rebase first. Like `new`, it needs a clean git workspace, untracked files included, and checks
it even with `--dry-run`.

A dry run prints the plan like this, with the base resolved to the commit the first step uses; the later steps find
it in the changelist's record. The message, which only the first step gets, is printed below it:

```
abc1234 First commit: git p4son new my-feature --review -b 5f1d0e9c2a7b4e8d9c0b1a2f3e4d5c6b7a8f9e0d
def5678 Second commit: git p4son update my-feature --shelve
ghi9012 Third commit: git p4son update my-feature --shelve
```

If a step fails, `review` returns to where you started and names the step and commit that failed. Once the first
step has created the changelist, the error also says how to finish: after fixing the problem, run the
`git p4son update my-feature --shelve --per-commit` it prints, which resumes after the last commit that worked, as
the changelist's record says which commits it includes.

### List-Changes Command

List the commit subjects since a base branch, oldest first and numbered, as a changelist description lists them:

```sh
git p4son list-changes [--base-branch BASE_BRANCH] [--porcelain]
```

**Options:**
- `-b, --base-branch BASE_BRANCH`: Base branch to compare against. Default is `HEAD~1`.
- `--porcelain`: Print only the numbered list, and nothing when there are no changes, for scripts and agents. Errors
  still go to stderr.

**Examples:**
```sh
git p4son list-changes
git p4son list-changes --base-branch main
git p4son list-changes --base-branch main --porcelain
```

### Alias Command

Manage changelist aliases: names that stand for changelist numbers, so you can say `my-feature` instead of `12345`.
`new` and `review` save one named after the branch, which also records what [`update`](#update-command) needs to go
on from. Aliases are stored in `.git-p4son/aliases/`; ones saved by older versions in `.git-p4son/changelists/` keep
working, and move when written, or all at once with `alias clean`. Alias names ignore case: they're saved in lowercase,
so `My-Feature` and `my-feature` are the same alias on every file system.

#### alias list

List all aliases and their changelist numbers:

```sh
git p4son alias list
```

#### alias show

Print the changelist number saved under an alias:

```sh
git p4son alias show [alias]
```

Only the number is printed to stdout, so scripts and AI agents can capture it, e.g. to find the changelist that
`new` or `review` created. Exits with an error if the alias does not exist.

**Arguments:**
- `alias` (optional): Alias name to show. Defaults to the current branch name.

**Examples:**
```sh
git p4son alias show              # changelist for current branch
git p4son alias show myfeature
p4 describe -s $(git p4son alias show)
```

#### alias new

Save a changelist number under a named alias:

```sh
git p4son alias new <changelist> [alias] [--force]
```

The changelist must be pending, yours and on this workspace's client, as `update` can only edit those. A changelist
another user owns is refused, such as the copy Swarm keeps of a review, which usually takes the number just after yours
and is the one the review's URL shows.

**Arguments:**
- `changelist`: Changelist number to save
- `alias` (optional): Alias name to save the changelist number under. Defaults to the current branch name.

**Options:**
- `-f, --force`: Overwrite an existing alias file

**Examples:**
```sh
git p4son alias new 12345              # alias defaults to branch name
git p4son alias new 12345 myfeature
git p4son alias new 67890 myfeature -f
```

#### alias rename

Rename an alias, keeping its changelist and everything it records for `update`:

```sh
git p4son alias rename [OLD] NEW [--force]
```

As with `git branch -m`, a single name is the new name for the current alias, the one the current branch names, or
with jj the one that records the jj change. The `branch` keyword works for either name; as `NEW` it is the current
branch or bookmark name, so the alias can follow a renamed branch.

**Arguments:**
- `OLD` (optional): The alias to rename. Defaults to the current alias
- `NEW`: The new alias name

**Options:**
- `-f, --force`: Overwrite an existing alias of the new name

**Examples:**
```sh
git p4son alias rename my-feature-v2              # the current alias, then: git branch -m my-feature-v2
git p4son alias rename my-feature branch          # after git branch -m: follow the branch's new name
git p4son alias rename my-feature my-feature-v2
```

With git, renaming the branch first means the branch no longer names the alias, so name both:
`alias rename OLD branch`. With jj, `alias rename NEW` works before or after `jj bookmark rename`, as the alias
records the jj change.

#### alias delete

Delete a changelist alias, or all of them:

```sh
git p4son alias delete [alias | --all]
```

**Arguments:**
- `alias` (optional): Alias name to delete. Defaults to the current branch name.

**Options:**
- `--all`: Delete all aliases without prompting, whether their changelists are pending or not. To delete only those
  that are not, use `alias clean`. An alias that can't be deleted is reported and skipped, and the command then exits
  with 1

**Examples:**
```sh
git p4son alias delete              # delete alias for current branch
git p4son alias delete myfeature
git p4son alias delete --all
```

#### alias clean

Delete the aliases whose changelist is no longer pending, as it was submitted or deleted:

```sh
git p4son alias clean [-n] [--porcelain]
```

It first renames aliases an older version saved under a name that isn't lowercase, or is reserved once lowercased (such
as `HEAD`), to their lowercase name, and moves any aliases still in `.git-p4son/changelists/` to `.git-p4son/aliases/`
the same way. A name already taken, or reserved, gets `-1`, `-2` and so on appended, so `HEAD` becomes `head-1`. Until
then, such an alias isn't listed or found, except one only in another case on Windows or macOS. Each rename is listed.
Then it deletes any alias whose file holds no changelist, and goes through the rest in name order: it looks up the
alias's changelist in Perforce and deletes the alias if the changelist was submitted or doesn't exist. If p4 answers
neither that nor pending for a changelist, it stops there, leaving that alias and the ones after it. A submit that
renumbers the changelist leaves its old number behind, which counts as gone too. With the jj backend, an alias whose [jj
change](#jujutsu-jj) is gone but whose changelist is still pending is kept; `alias list` marks it `(gone)`, and `alias
delete` removes it.

It ends under an `Alias clean done` heading with how many aliases it deleted and kept. If any couldn't be deleted,
that line is a warning with how many failed, followed by an error, and the command exits with 1.

To delete specific aliases, use `alias list` and `alias delete`.

**Options:**
- `-n, --dry-run`: List the aliases that would be deleted, without deleting, renaming or moving any. Ends with
  `Dry run, nothing changed`
- `--porcelain`: Print only one `<action> <alias> <CL>` line per alias, for scripts and agents. The action is `kept`,
  `deleted`, or `failed` when the alias couldn't be deleted; with `-n` it is `would-keep` or `would-delete`. An alias
  whose file holds no changelist has `-` for its CL. Errors still go to stderr. A `failed` alias makes the exit code 1,
  after the lines for all aliases; a stop exits with 1, with no line for the alias it stopped at or those after it.

**Examples:**
```sh
git p4son alias clean -n            # see which aliases would go
git p4son alias clean
git p4son alias clean --porcelain   # e.g. "deleted feature-login 12345"
```

### Review-Tool Command

Show or set the default [review tool](#review-tools), and the tool an alias records for its changelist's review:

```sh
git p4son review-tool                          # list the tools, and show the current alias's tool
git p4son review-tool list                     # list the tools, marking the default
git p4son review-tool default                  # show the default tool
git p4son review-tool default my-tool          # make my-tool the default
git p4son review-tool show [alias]             # show the tool an alias records
git p4son review-tool set my-tool [alias]      # record my-tool in an alias
```

- `default` sets `[review-tool] default` in `.git-p4son/config.toml`, refusing a tool that isn't configured. It
  edits only that line, so the rest of the file keeps its comments and layout. Without the setting, Swarm is the
  default. A review keeps the tool it was created with.
- `set` records the tool of a review that exists, such as one whose alias was recreated with `alias new`, or one
  created by hand, so `update --shelve` runs the tool's `update` arguments after each shelve. It keeps everything else
  the alias records.
- `show` prints the tool and whether it has run for the review, or `None recorded`.
- The alias is the last argument and defaults to the current one, as with `alias new`.

### Writable Command

Writable mode keeps git-tracked files writable, so you can edit them without running `p4 edit` first. git-p4son
opens changed files in Perforce itself when you run `new`, `update` or `review`. Git-ignored files, such as content
and other Perforce-only files, stay read-only and are checked out through Perforce as usual.

```sh
git p4son writable            # Show whether writable mode is on
git p4son writable enable     # Turn writable mode on, then apply it
git p4son writable disable    # Turn writable mode off, then apply it
git p4son writable apply      # Make git-tracked files match the current mode
```

`apply` makes every git-tracked file writable when the mode is on. When it's off, it makes them read-only, except
files opened in Perforce in any changelist. On a workspace with the `allwrite` option, making files read-only is
skipped, since the client spec wants every file writable. Git-ignored files are never changed.

The setting is stored in `.git-p4son/config.toml`:

```toml
[core]
writable = true
```

With the mode on, `sync` makes the files it synced writable again if git tracks them, since Perforce writes synced
files read-only. Files made read-only outside git-p4son, for example by submitting from P4V, `p4 revert`, or "Get
Latest" in P4V, stay read-only until a later sync touches them. Run `git p4son writable apply` to fix them right
away.

This replaces the `post-sync` hook some users run to make every tracked file writable after a sync.

Turning the mode off also makes tracked files whose Perforce type carries the `+w` (always writable) modifier
read-only. The next sync of such a file makes it writable again.

### Completion Command

Print the path to a shell completion script:

```sh
git-p4son completion <shell> [--dirname]
```

**Arguments:**
- `shell`: Shell to print completion script path for (`bash`, `zsh`, or `powershell`)

**Options:**
- `-d, --dirname`: Print the directory containing the completion script instead of the full file path

See [Shell Completions](#shell-completions) below for installation instructions.

### Skill Command

Install or show the [Claude Code skill](#claude-code-skill):

```sh
git p4son skill install    # install the skill for your user
git p4son skill show       # print the instructions the skill gives to AI agents
```

`show` also prints the workspace's own [overrides](#workspace-overrides) from `.git-p4son/skill-override.md`, if it
has any.

### The `branch` keyword

Most commands that accept an alias or changelist argument default to the current branch name. You can also pass the
`branch` keyword explicitly - it resolves to an alias name derived from the current git branch.

The keyword cannot be used in a detached HEAD state. Use `--no-alias` (on `new`) or supply an explicit
alias name instead.

## Configuration

git-p4son keeps its per-repository settings in `.git-p4son/config.toml`. `init`, `writable` and `sync-split-users`
write most of them; the rest you edit by hand.

- `[depot] root` (string, set by `init`): the depot path `sync` syncs, e.g. `//$(workspace)` or
  `//$(workspace)/Engine`. See [Init Command](#init-command)
- `[core] writable` (`true` or `false`, default `false`): [writable mode](#writable-command), which keeps
  git-tracked files writable
- `[core] backend` (`"auto"`, `"git"` or `"jj"`, default `"auto"`): the tool git-p4son follows for the change, the
  base and the branch. `auto` means jj in a [jj workspace](#jujutsu-jj) and git otherwise
- `[sync] branch` (string, set by `init`): the sync branch, which `sync` commits onto from whatever branch you are
  on. Without it, `sync` uses `main`. See [Sync Command](#sync-command)
- `[sync] split-users` (list of strings, default empty): the users whose changelists `sync` gives a commit each;
  `$(user)` stands for you. See [Split users](#split-users)
- `[sync] blocking-processes` (list of strings, default empty): processes that stop a sync while they run. See
  [Blocking processes](#blocking-processes)
- `[review-tool] default` (string, default `"swarm"`): the [review tool](#review-tools) `new --review` and `review`
  push to without `--review-tool`
- `[review-tool.<name>]` (table): a [review tool](#review-tools)'s `command`, and its `new` and `update` arguments
- `[core.script-runners]` (table, Windows only): the command that runs a hook script, by file extension.
  See [Hooks](#hooks). The old `[hooks.extension-associations]` is still read, and `core.script-runners` wins where
  both name an extension

For example:

```toml
[depot]
root = "//$(workspace)"

[core]
writable = true

[sync]
split-users = ["$(user)", "alice"]
blocking-processes = ["UnrealEditor"]
```

A value of the wrong type, such as `writable = "true"` or `split-users = "alice"`, or a file that is not valid TOML,
stops the command with an error naming the setting, so a typo is never silently ignored.

## Review Tools

`new --review` and `review` create a review in Swarm by default: the description gets a `#review` keyword, and Swarm
picks up each shelf. A team that reviews with another tool, one that has to be run to post each shelf, configures the
tool's command line under a name of its choosing in `.git-p4son/config.toml`:

```toml
[review-tool]
default = "my-tool"

[review-tool.my-tool]
command = ["python", "tools/my-review.py", "--p4port", "$(p4port)", "--changelist", "$(changelist)"]
new = ["create"]
update = ["update"]
```

`--review-tool my-tool` pushes a review to it, and `default` makes it the default, so `--review-tool swarm` picks
Swarm again. The alias records the changelist's tool by name, Swarm included, so `update --shelve` runs it again
without naming it, and a review stays on its tool when the default changes. `update` never falls back to the default:
an alias that records no tool, such as one made by `alias new`, gets no tool run until
[`review-tool set`](#review-tool-command) records one. `--no-alias` is refused with any tool but Swarm.

With a review tool other than Swarm, there is no `#review` keyword, and after each shelve of the review git-p4son runs
`command` from the workspace root, once per commit with `review`. On the shelve of `new`, the `new` arguments are
added to it, to create the review, and on the shelves of `update` the `update` ones, to add the new shelf as a
revision of it. If the first run fails, the `update --shelve` that reruns it still gets the `new` arguments, as the
review doesn't exist yet. Both are optional; without one, `command` runs as it is.

`command`, `new` and `update` are lists, one string per argument; a single string is refused. No shell runs them,
so they work the same on every platform and never need quoting. A program named by a relative path, such as
`tools/review.exe`, is found from the workspace root, and a bare name, such as `python`, on `PATH`. A `.cmd` or `.ps1`
script on Windows needs its interpreter named, such as `["pwsh", "-File", "tools/my-review.ps1"]`. `$(name)` in an
argument is replaced:

- `$(changelist)`: the pending changelist number
- `$(workspace)`: the Perforce client workspace name
- `$(user)`: the Perforce user
- `$(p4port)`: the server address, as `p4 set P4PORT` gives it
- any other name: the environment variable of that name, which on Windows ignores case. A name that is neither fails
  the command before anything runs

A replaced value stays within its argument, spaces and all. The command is echoed with the values in, so it can be
rerun by hand; that also shows any token passed through an environment variable. Anything more, such as settings
files or a sign-in, goes in a wrapper script of your own that the command runs.

The first run can sign in or open a browser; later ones run once per shelve, often as the steps of a per-commit run,
so keep them non-interactive. A tool that exits with a non-zero code fails the command. `review` then stops at that
commit with the command that resumes it, which shelves the commit again and reruns the tool, and `new` names the
`update <alias> --shelve` that reruns it once the tool works. `--dry-run` says which tool would run. The tool runs on
the terminal, so its output shows as it is written and it can prompt. Like a hook, it runs under git-p4son's
[jj lock](#locking-and-the-git-workflow), so it must not run jj commands that snapshot.

## Hooks

Like git hooks, hooks are programs git-p4son runs at set points of a command: executables, or on Windows scripts, in
`.git-p4son/hooks/<hook-name>/`, run in filename order.

A hook runs in the directory git-p4son was run from, with `GIT_P4SON_REPO_ROOT_DIR` set to the repository root and
`GIT_P4SON_BACKEND` to `git` or `jj`, the [backend](#choosing-the-backend) the command runs with.

On Windows, `.ps1`, `.sh` and `.py` files run with `powershell.exe -NoProfile -ExecutionPolicy Bypass -File`,
`bash.exe` and `python.exe`. Change or add script runners in `.git-p4son/config.toml`:

```toml
[core.script-runners]
".ps1" = ["pwsh.exe", "-NoProfile", "-File"]
".nu" = "nu.exe"
```

Non-executable or unrecognized hook files, and invalid runner entries, are skipped with a warning.

### pre-sync hook

Before `git-p4son sync` syncs any changelist, git-p4son runs executable hooks from
`.git-p4son/hooks/pre-sync/`. All of them run, and if any exits with a non-zero code the sync is aborted before
any changelist is synced. When a sequence of changelists is synced, the pre-sync hooks run only once,
before the first sync.

### post-sync hook

After a successful `git-p4son sync` that actually performs sync work, git-p4son runs executable hooks from
`.git-p4son/hooks/post-sync/`. They don't run when the workspace is already at the requested changelist.

## Jujutsu (jj)

git-p4son detects a jj workspace colocated with the git repository, which needs jj 0.41 or newer. CI tests both
0.41 and the latest jj release, and the minimum only rises with a note in the CHANGELOG. Separate jj workspaces,
sparse working copies, and conflicted or divergent changes are not supported. Without a `.jj` folder, nothing
changes.

### The jj p4son alias

jj has no external subcommands, but an alias can run git-p4son, so `jj p4son sync` runs `git p4son sync`. Add it
once per machine to jj's user config, or let `init` offer it. From jj 0.42, an alias can carry a doc, which jj's
shell completion shows next to `p4son`:

```sh
jj config set --user aliases.p4son '{ definition = ["util", "exec", "--", "git-p4son"], doc = "Run git-p4son" }'
```

With jj 0.41, set `aliases.p4son` to the list alone, `'["util", "exec", "--", "git-p4son"]'`. `init` writes
whichever form the installed jj supports.

`jj util exec` neither locks nor snapshots, so `jj p4son` behaves exactly as `git p4son`, and git-p4son's messages
keep saying `git p4son`. Run it from inside the workspace: `jj -R other-workspace p4son` would still run in the
current directory, so git-p4son refuses with "Run jj p4son from inside the workspace; -R is not supported." Shell
completion for `jj p4son` needs [a hook of its own](#jj-p4son).

### Choosing the backend

The backend is the tool git-p4son follows for the change, the base and the branch: git or jj. By default it is jj
in a jj workspace and git otherwise. To choose, set `core.backend` in `.git-p4son/config.toml`, or put the global
`--backend` flag before the command, which overrides the setting:

```toml
[core]
backend = "git"   # "auto" (the default), "git" or "jj"
```

```sh
git p4son --backend git new -b main
```

`jj` outside a jj workspace is an error. In a jj workspace, the jj backend refuses to run when jj is missing or
older than 0.41, and points to `--backend git`. The git backend needs no jj, and still holds jj's working-copy
lock, which jj 0.41 or newer honours, so jj cannot snapshot halfway through a command.

With the jj backend in a jj workspace:

- `new`, `update` and `list-changes` work on the jj working change `@`. When `@` is empty, has no description or
  bookmark and has one parent, they work on that parent instead, so a fresh working change above a feature still
  refers to the feature.
- `new` records the change's ID in the alias it saves, so later commands find the alias by change, whatever the
  change's bookmarks or description have become since. A new alias is named after the one local bookmark at the
  change, with `/` replaced by `-`, or, with no bookmark or several, after the first 8 letters of its change ID.
- When no alias records the change itself, as after adding changes on top of the one `new` recorded, the current
  alias is the one recording the nearest change below it. The search stops at the first bookmark below the change,
  so a bookmarked change stacked on another's CL doesn't take that CL's alias, and skips empty changes, which a sync
  leaves behind for submitted CLs. In a stack without bookmarks, every change above `main` counts. jj doesn't move
  a bookmark on `jj new`, so a bookmark left on the change `new` recorded stops the search from the changes above
  it: move it to the tip with `jj bookmark move <name> --to @-`, or `--to @` when the working change has content.
- An alias records one change, and a change one alias: `new` refuses a change another alias records, naming it, and
  `--force` moves the record to the new alias. `alias new` records no change. `alias list` shows each recorded change,
  marked `(gone)` once jj no longer has it.
- The default message is the first line of the change's description. Use `jj describe`, `-m` or `-F`.
- `-b/--base-branch` takes a jj revision that resolves to one change, such as a bookmark, change ID or commit ID.
  Where it has a default, it's the change's parent rather than git's `HEAD~1`. `new` and `review` record it as a
  bookmark when it names one or resolves to a commit exactly one points at, and as its change ID when it has none, so
  a local change kept under a stack, never submitted, can be the base. Several bookmarks at it is an error.
- `new` and `update` use jj's snapshot of the working copy, check that it did not change while git-p4son took the
  lock, and refuse files jj left untracked. You do not need a separate committed change before opening files in
  Perforce. Before a Perforce revert overwrites a file, git-p4son saves the working file and puts it back
  afterwards, keeping its bytes, line endings and permissions instead of taking git's index version.
- `init` holds jj's working-copy lock while it sets up the git repository. It also offers to colocate jj and to
  add the `jj p4son` alias, see [Init Command](#init-command).

For example:

```sh
jj git init --colocate       # Add jj to an existing git-p4son repository (see Line endings), or answer yes in init
jj p4son sync                # From any change: syncs main, then rebases your changes onto it

jj new main -m "Fix login"
# Edit files
jj bookmark create fix-login
jj p4son new -b main --review
# Edit more files
jj p4son update --shelve
```

### Syncing a bookmark

`sync` works from any change, as with git. It moves the sync bookmark, `[sync] branch` or `--branch`, defaulting
to `main`, to each sync commit:

1. It puts the bookmark's files on disk. Your working change keeps its content in jj, which snapshotted it first.
2. It syncs and commits, then releases jj's lock.
3. It rebases the changes you started from onto the moved bookmark, as `jj rebase -b <your change> -d main`, and
   lists any change that got conflicts, which jj records rather than stopping on.
4. It returns you to where you started: `jj edit` on the change you were editing, or `jj new` above it.

From the sync bookmark itself, nothing is rebased. `--no-rebase` skips the rebase and still returns you.

`sync` refuses when the sync bookmark points at the working change itself, which a sync would make part of the
bookmark's history. Move the bookmark back with `jj bookmark set main -r @- --allow-backwards`, or, to keep the edits
as a change on it, run `jj describe` and `jj new`.

You stay on the sync bookmark when:

- local changes were merged back: they are in a new working change on it, for you to review, and your changes are
  still rebased
- a sync fails partway: jj's next command puts a new working change there, holding the files as Perforce left them,
  and nothing is rebased
- the rebase fails: sync reports it with the `jj rebase` command to run and a non-zero exit code, and the sync is
  not undone

A failing pre-sync hook, or anything else before the first sync, puts your working change's files back.

`sync` still stages and commits with git, including git attributes and filters, and sets `core.autocrlf` to match
jj's line ending conversion.

### Line endings

jj writes the working files by its `working-copy.eol-conversion`, and git by its `core.autocrlf`. When the two
disagree, jj and git see different line endings, and jj fills changes with line ending edits. Set them to go
together:

| git's `core.autocrlf` | jj's `working-copy.eol-conversion` |
| --- | --- |
| `false`, the default | `none`, the default |
| `true`, the default in Git for Windows | `input-output` |
| `input` | `input` |

On the jj backend, git-p4son warns when they disagree, and names the jj setting that matches git, for example
`jj config set --repo working-copy.eol-conversion input-output`. The check reads only `core.autocrlf`: `text` and
`eol` attributes in a `.gitattributes` override it in git, and jj doesn't read them.

Set it before jj's first snapshot, as `jj git init --colocate` takes one. Once jj has recorded the files with the
wrong line endings, a later setting doesn't fix them, as jj only reads the files again when they change. `init`
colocates with the matching setting, and to colocate by hand, pass it to the init too:

```sh
jj --config working-copy.eol-conversion=input-output git init --colocate
jj config set --repo working-copy.eol-conversion input-output
```

If you colocated without it, and jj shows every text file as changed, colocate again before doing anything else in
jj. Don't `jj restore` that change: it holds every file the first snapshot added, and restoring it deletes them from
disk, including your untracked work. Colocating again leaves the files alone:

```sh
rm -rf .jj                   # Remove-Item -Recurse -Force .jj in PowerShell
git reset                    # Drops the entries jj added to git's index
jj --config working-copy.eol-conversion=input-output git init --colocate
jj config set --repo working-copy.eol-conversion input-output
```

If git ignores folders only by matching without case, see [Ignored files](#ignored-files): `git p4son init`
colocates with both settings.

### Ignored files

git matches `.gitignore` patterns without case when `core.ignorecase` is true, the default on Windows and macOS. jj
always matches them with case, so it would track a `Thirdparty/` folder that git ignores through `ThirdParty/`.

git-p4son keeps jj from tracking them by leaving each such folder, or file, out of jj's `snapshot.auto-track` for the
repository, building on any value you have, for example `(all()) ~ root:Some/Plugin/Source/Thirdparty`. `jj status`
then lists them under "Untracked paths". It does so when `init` colocates, and at the end of each sync on the jj
backend, as a sync can bring in new folders. To let jj track such a folder again, remove its `root:"..."` with
`jj config edit --repo`. As the repository's value holds yours, a later change to your user-level
`snapshot.auto-track` doesn't reach the repository.

If jj tracks such files anyway, for example in a workspace colocated before git-p4son did this, git-p4son warns about
the ones your change adds. The warning lists the `jj config set` that leaves them out and the `jj file untrack` that
stops tracking them, which leaves the files on disk. Both are quoted to paste into cmd, PowerShell or a POSIX shell.
They name a folder only when git tracks nothing else in it, so files git tracks stay tracked. Files you track on
purpose, with `jj file track --include-ignored`, are left alone.

### Locking, and the git workflow

git-p4son holds jj's working-copy lock while commands run, including sync hooks and temporary Perforce reverts,
so other jj processes cannot snapshot files halfway through. A busy lock stops the command. The lock does not keep
out editors, git commands or other jj workspaces, and a command is not one atomic jj transaction: jj's first
snapshot, the final import, and sync's rebase and return happen outside it. If a command fails, look at the workspace
before continuing: Perforce operations that completed are not undone.

Hooks must not run ordinary jj commands, which would wait for the lock their caller holds. Read-only queries such
as `jj --at-op=@ log` are fine, and a hook that runs git-p4son itself needs `--backend git`, which reuses the
caller's lock.

`review`, `new --per-commit` and `update --per-commit` run once per commit since the base, as they do in git. Under
jj's lock, each step puts its commit's files on disk with git, and the working change's files go back at the end,
also when a step fails, so jj sees no change. Untracked and ignored files are left alone. They refuse to start if
files changed after jj's snapshot, as the checkouts would overwrite them:

```sh
git p4son review -b main
git p4son update --per-commit --shelve
```

To use the git workflow on purpose, put `--backend git` **before** the command. This uses git branches, `HEAD` and
git's clean-workspace check.

## Claude Code Skill

git-p4son ships a skill for Claude Code that teaches it how git-p4son is meant to be used: keeping `main` in sync
with Perforce, never submitting on your behalf, creating reviews with full control of the changelist description,
updating them after feedback, and cleaning up after a submit. With it installed, you can
ask Claude to sync, put a branch up for review or address review feedback, and it will use git-p4son to do it.

Install it once for your user:

```sh
git p4son skill install
```

This writes `~/.claude/skills/git-p4son/SKILL.md` (under `$CLAUDE_CONFIG_DIR` if set). `init` offers to do the same.
The skill is installed for your user rather than in the workspace, where it would show up as an untracked file and
block `sync`, `new` and `update`.

The installed file is only a short stub: it tells Claude when the skill applies and to run `git p4son skill show`,
which prints the full instructions from the installed git-p4son. So the instructions always match your version of
git-p4son, and upgrading with pip needs no reinstall. Run `git p4son skill show` yourself to read what Claude is told.

### Workspace overrides

To teach Claude something that only holds for one workspace, such as a team's submit conventions, write it in
`.git-p4son/skill-override.md`. Run inside that workspace, `skill show` prints it after the shipped instructions,
under a heading saying it takes precedence where the two disagree. Start your own headings at `###`, since they land
under that `##` heading. The file lives in `.git-p4son/`, so git ignores it and reinstalling the skill leaves it alone.

Claude picks the skill up on its own when you work in a git-p4son workspace. You can also invoke it directly with
`/git-p4son`.

## Shell Completions

Tab completion is available for bash, zsh, and PowerShell, including commands, flags, and dynamic alias names.

### bash

Add the following to `~/.bashrc`:

```bash
source $(git-p4son completion bash)
```

### zsh

Add the following to `~/.zshrc` before `compinit`:

```zsh
fpath=($(git-p4son completion -d zsh) $fpath)
autoload -Uz compinit && compinit
```

### PowerShell

Add the following to your PowerShell profile (`$PROFILE`):

```powershell
. $(git-p4son completion powershell)
```

All three enable completion for `git p4son <TAB>` and `git-p4son <TAB>`.

### jj p4son

jj completes an alias's arguments as file paths, so `jj p4son <TAB>` needs a hook of its own. It completes the words
after `p4son` as `git p4son` does, skipping jj's global options such as `-R` or `--color` before it, and hands every
other `jj` completion to jj's own. Load it after jj's completion; the PowerShell hook needs PowerShell 7 (`pwsh`):

```bash
source $(git-p4son completion bash --jj)        # ~/.bashrc
```

```zsh
source $(git-p4son completion zsh --jj)         # ~/.zshrc, after compinit
```

```powershell
. $(git-p4son completion powershell --jj)       # $PROFILE
```

Each loads the `git p4son` completion for its shell too. If you named the alias something other than `p4son`, set
`GIT_P4SON_JJ_ALIAS` to its name.

## Development

To install from source in editable mode:

```sh
git clone https://github.com/neoboid/git-p4son.git
cd git-p4son
pip install -e .
```

The `-e` flag installs the package in "editable" mode, meaning changes to the code are immediately available
without reinstalling. This is also handy if you want to auto-update git-p4son whenever you pull from GitHub.

git-p4son only uses Python standard library modules - no additional packages are required.

Run the tests with `python -m pytest tests/` (`pip install pytest`). With `pytest-xdist` installed, add
`-n auto --maxprocesses 8` to run them in parallel, which is about three times faster.

### Git Hooks

The repository includes git hooks in the `hooks/` directory:

- **pre-commit** - verifies that staged Python files are formatted with `autopep8`. Install it with
  `pip install autopep8`.
- **pre-push** - runs the test suite and blocks the push if any test fails. It needs `pytest` installed, and runs
  the tests in parallel when `pytest-xdist` is too.

To activate the hooks, run:

```sh
scripts/setup-hooks.sh
```

This configures `core.hooksPath` so git uses the hooks from the repository. You only need to run this once after
cloning.

