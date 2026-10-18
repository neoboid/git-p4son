# Git Perforcesson

`git-p4son` is a tool for managing a local git repository within a Perforce workspace.
This way you can use the familiar local git development flow even though you are
working on a project where perforce is used for source control.

The idea is to have a `main` git branch that is kept in sync with a branch of a Perforce depot.
From `main` you branch out into feature branches, where you do local changes
and rebase on `main` whenever it is updated.

Once your changes is ready to be submitted in perforce `git-p4son` can help you create both
changelists and push them for reviews. You can even push individual git commits as patches
for review, so that the reviewer can see the changes in the order you made them locally. Similar
to the pull request worfkflow on github.

This is a bit cumbersome to do manually, but this package provides commands
that help out with the repetitive and error prone stuff.

## On big workspaces
Sometimes git struggles to keep entire perforce workspaces in the repo, like when working
on a Unreal Engine game with all source code in the workspace. If so, it helps to edit
`.gitignore` and include only the subdirectories in which you work, the rest of the files
will still be managed by perforce of course.

## Usage Example

Here's a typical workflow when developing a feature using `git-p4son`:

```sh
# Sync main with latest changes from perforce
git checkout main
git p4son sync

# Start work on a new feature
git checkout -b my-fancy-feature

# Change some code
git add .
git commit -m "Feature part1"

# Sync to the latest changelist affecting the workspace
git checkout main
git p4son sync

# Rebase your changes on main
git checkout my-fancy-feature
git rebase main

# Change even more code
git add .
git commit -m "Feature part2"

# Create a Swarm review with all commits since main in one go.
# Each commit is checked out in turn and gets a shelf of its own in the
# review, then you are back on your branch.
# An alias called "my-fancy-feature" (derived from the branch name) is
# saved so you can refer to this CL by name in follow-up commands.
git p4son review -m "My fancy feature" -b main

# After review feedback, make more changes
git add .
git commit -m "Address review feedback"

# Update the changelist with latest commit, re-open files, and re-shelve.
# The changelist is looked up by branch name automatically.
git p4son update --shelve

# After approval, submit in p4v

# Sync to the latest changelist from perforce
git checkout main
git p4son sync

# Force remove old branch as you don't need it anymore
git branch -D my-fancy-feature

# Remove changelist alias for the branch
git p4son alias delete my-fancy-feature

# Start working on the next feature
git checkout -b my-next-fancy-feature
```

## Installation

```sh
pip install git-p4son
```

Requires Python 3.11 or later.

### Updating

```sh
pip install --upgrade git-p4son
```

For tab completion (zsh and PowerShell), see [Shell Completions](#shell-completions). To let Claude Code drive
git-p4son, see [Claude Code Skill](#claude-code-skill).

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

## Setup

These steps set up git-p4son in an existing Perforce workspace. You only need to do this once.

1. **Set a git editor** if you don't have one already, for git's commit messages. `init` warns when none is set.
   Set one with:
   ```sh
   git config --global core.editor <editor>   # e.g. vim, nano, code --wait
   ```

2. **Sync your workspace to a known changelist.** Pick a changelist to use as the starting point for your git
   history:
   ```sh
   p4 sync //...@12345
   ```

3. **Run `git p4son init`.** This can be anywhere inside your Perforce workspace - it doesn't have to be at the
   root:
   ```sh
   cd /path/to/your/workspace    # or a subdirectory of it
   git p4son init
   ```
   The command verifies that you are inside a P4 workspace, prompts you to select a depot
   root (entire workspace or current directory subtree), runs `git init`, sets up `.gitignore` (copying from
   `.p4ignore` if available), and creates an initial commit.

4. **Review `.gitignore`.** Edit the file to ensure build artifacts and other unwanted files are excluded.

5. **Add and commit all files manually the first time**
   ```sh
   git add .
   git commit -m "Initial submit all files"
   ```

6. **Run `git p4son sync`** to get your first official sync commit
   ```sh
   git p4son sync
   ```
   This creates a commit with message that records the CL you have synced.

From here, branch off `main` for local development. See the [Usage Example](#usage-example) for a typical
workflow.

### How sync handles writable files

When you switch branches with `git checkout`, git removes the read-only flag on every file it writes. This means
that when you switch back to `main` for a sync, Perforce may refuse to overwrite those files because they are
writable. git-p4son handles these automatically by classifying each writable file:

**writable-unchanged** - The file on disk is identical to the last sync (only the read-only flag was removed
by git). This is the common case after switching branches. git-p4son detects this by comparing git blob IDs against
the last sync commit that touched the file, restores the read-only flag, and lets the sync overwrite the file.

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
- `[hooks.extension-associations]` (table, Windows only): the command that runs a hook script, by file extension.
  See [Hooks](#hooks)

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

## Usage

git-p4son provides eleven commands: `init`, `sync`, `sync-split-users`, `new`, `update`, `review`,
`list-changes`, `alias`, `writable`, `completion`, and `skill`.

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

Initialize a git repository inside a Perforce workspace:

```sh
git p4son init
```

This command checks that it runs in a Perforce workspace, then asks for the depot root, writable mode, split users,
the Claude Code skill and jj. It then sets up `.gitignore`, runs `git init` and creates a first commit holding only
`.gitignore`. It ends by listing the next steps, and warns when no git editor is configured.

The depot root determines which part of the Perforce workspace git-p4son syncs. You can choose to sync the entire
workspace or just the directory's subtree where the git root is placed. The selection is saved in
`.git-p4son/config.toml` and used by all subsequent commands.

Nothing in `.git-p4son/` belongs in git. The folder has its own `.gitignore` that ignores everything in it, so there
is no need to list it in the workspace `.gitignore`.

The saved root uses a `$(workspace)` placeholder for the workspace (client) name, e.g.
`root = "//$(workspace)/Engine"`. git-p4son substitutes the live workspace name each time it runs, so renaming
the Perforce workspace does not break the config. You can still store a concrete name (e.g. `//my-workspace/Engine`)
by hand-editing `config.toml`; both forms work.

The `.gitignore` is set up using this priority:
- If `.gitignore` already exists, it is left as is
- If `.p4ignore` exists, it is copied to `.gitignore` as a starting point
- Otherwise, an empty `.gitignore` is created

`init` also asks whether to turn on [writable mode](#writable-command), defaulting to the current setting. It only
saves the answer; run `git p4son writable apply` once your files are committed to update their permissions.

`init` then asks whether to sync your own changelists individually, making you a [split user](#split-users).
The default is the current setting. Yes adds a `$(user)` entry to the split users and no removes it; other split
users are left alone.

`init` records the [sync branch](#sync-command) as `[sync] branch`. In a repository it creates, that's the branch
`HEAD` is on. In an existing one, it's `main` if there is one, else the branch `HEAD` is on, else one you pick from
the local branches. It keeps a branch already set.

If Claude Code is installed, `init` offers to install the [Claude Code skill](#claude-code-skill). An
already installed skill is refreshed without asking.

With jj 0.41 or newer installed, `init` offers two things for [jj](#jujutsu-jj):

- **Colocating jj**, when the workspace has no `.jj` folder: "Use jj in this workspace too (colocated)?", default
  no. On yes, in an existing repository it runs `jj git init --colocate` as its last step, so jj imports the branch
  as a bookmark. In a repository it creates, it lists `jj git init --colocate` as a next step after your initial
  commit instead, since jj would otherwise snapshot every file before you have reviewed `.gitignore`.
- **The [`jj p4son` alias](#the-jj-p4son-alias)**, when the workspace has a `.jj` folder, or is to get one, and
  jj's config has no `aliases.p4son`: default yes. It adds the alias to jj's user config.

Running `init` again asks neither once they are done.

### Sync Command

Sync local git repository with a Perforce workspace:

```sh
git p4son sync [changelist ...] [--branch BRANCH] [--force] [--dry-run] [--split-user NAME ...] [--no-split]
               [--ignore-blocking-processes]
```

`sync` commits onto the sync branch, `[sync] branch` in `.git-p4son/config.toml`, which `init` sets and which
defaults to `main`. From another branch, it checks the sync branch out, syncs and commits, then returns you to
where you started, telling you to `git rebase` your branch onto the sync branch; it doesn't rebase for you. The
pre-sync and post-sync hooks run on the sync branch. You stay on the sync branch when:

- local changes were [merged back](#how-sync-handles-writable-files) into the work tree, for you to review and
  commit there
- a sync fails partway, with the files as Perforce left them and the commits so far kept

A failing pre-sync hook, or anything else that stops `sync` before it syncs, returns you to where you started.

**Arguments:**
- `changelist` (optional): Changelist number, `last-synced` to re-sync the last synced changelist, or `head`
  to sync to the latest changelist. Omit to sync to the latest changelist affecting the workspace. Several
  strictly increasing changelist numbers may be given to sync each in sequence, producing one commit per
  changelist. A trailing `head` may close out such a sequence to finish at the latest changelist.

`sync` requires clean git and Perforce workspaces: no uncommitted changes, untracked files included, and no
git-tracked files opened in Perforce. It checks them after any [blocking processes](#blocking-processes) and before
the [pre-sync hooks](#pre-sync-hook).

**Options:**
- `--branch BRANCH`: Commit onto this branch instead of `[sync] branch`. With the jj backend, the bookmark to move
  to each sync commit
- `--no-rebase`: With the jj backend, leave the changes you started from where they are, instead of rebasing them
  onto the [sync bookmark](#syncing-a-bookmark)
- `-f, --force`: Allow syncing to changelists older than the current one
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
git p4son sync 12345 --force
git p4son sync --dry-run    # show the changelists a sync to latest would visit
git p4son sync -u alice     # also split out alice's changelists
git p4son sync --no-split   # sync without splitting out the configured split users
git p4son sync --branch p4  # commit onto the p4 branch this time
```

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
with `--force` are never split. Without any split users, `sync` makes no extra Perforce queries.

Finding the split users' changelists costs one extra Perforce query over the range being synced. It runs after the
blocking processes check, the clean-workspace checks and the pre-sync hooks, so a running blocking process, a dirty
workspace or a hook that aborts the sync says so before that work. `--dry-run` skips the checks and hooks but still
shows the resulting sequence.

#### Hooks

`sync` runs the [pre-sync](#pre-sync-hook) and [post-sync](#post-sync-hook) hooks.

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

Create a new Perforce changelist and add changed files to it. Description will contain an enumerated list of git commits
since the base branch. Optionally creates a Swarm review.

Opening files requires a clean git workspace, untracked files included; `new` refuses to run otherwise. The check
is skipped with `--no-edit`. After opening files, unchanged ones are reverted (see
[Keeping the changelist in sync with git](#keeping-the-changelist-in-sync-with-git)).

```sh
git p4son new -b BASE_BRANCH [-m <message> | -F <file>] [alias] [--force] [--dry-run] [--no-edit]
              [--no-alias] [--no-commit-list] [--shelve] [--review]
```

**Arguments:**
- `alias` (optional): Alias name to save the new changelist number under. Defaults to the current branch name.

**Options:**
- `-m, --message MESSAGE`: Changelist description message. Defaults to the HEAD commit subject
- `-F, --file FILE`: Read the changelist description message from a file instead of `-m`. The message may span
  multiple lines
- `--no-commit-list`: Leave the enumerated commit list out of the changelist description, so it holds only the
  message
- `-b, --base-branch BASE_BRANCH`: Required. The base the change starts from, for enumerating commits and finding
  changed files: a branch or bookmark, or a commit exactly one points at. The alias records it by name, as a commit
  would go stale after a sync and rebase, along with the commits since it the changelist includes, so `update`
  knows where the next ones start. With `--no-alias` any commit will do
- `-f, --force`: Overwrite an existing alias file
- `-n, --dry-run`: Pretend and print what would be done, but do not execute. Ends with `Dry run, nothing changed`
- `--no-edit`: Skip opening changed files for edit in Perforce
- `--no-alias`: Skip saving a changelist alias
- `--shelve`: Shelve the changelist after creating it
- `--review`: Add `#review` keyword and shelve to create a Swarm review

**Examples:**
```sh
git p4son new -m "Add feature" -b main
git p4son new -m "Fix bug" myalias -b main
git p4son new -m "Fix bug" --no-alias -b HEAD~1
git p4son new -m "New feature" --review -b main
```

### Update Command

Update an existing Perforce changelist with the commits it doesn't include yet. The message above the commit list
is kept unless `-m` or `-F` replaces it. By default also opens changed files for edit, and reverts files that are no
longer part of the git change (see
[Keeping the changelist in sync with git](#keeping-the-changelist-in-sync-with-git)). Opening files requires a
clean git workspace, untracked files included; `update` refuses to run otherwise. The check is skipped with
`--no-edit`.

**Changelists made by `new` or `review`.** Their alias records the base, by name, and the commits since it the
changelist includes, by their jj change IDs or, with git, their author dates, which survive rebases and amends.
`update` folds in the commits it doesn't know: it appends their subjects to the commit list, opens their files, and
records them. The entries the list already has are left as they are, so an entry you reworded in the changelist
stays as you wrote it. Every update counts, shelved or not.

- `-b` only chooses which files to open, from that commit on, for example `-b main` to open them all again after a
  manual `p4 revert`. The commit list is still only appended to. A `-b` above the last included commit is refused,
  as the commits it skips would be counted without being opened.
- An included commit that is gone from the branch, dropped or squashed into another, gets a warning, and the update
  goes on with the new commits. Its patch stays in the review: to undo a shelved commit, revert it in a new commit.
- A new commit below included ones is refused, as its own patch would undo the included commits above it. Move it
  after them, then update again.
- An amend, or a squash into an included commit, keeps the commit's identity, so `update` sees no new commit. With
  nothing new, `update` opens every file since the base, which takes in what the amend added. With new commits as
  well, run `update -b main` to open them.

**Other changelists**, whose alias records nothing (made by `alias new` or an older git-p4son), or named by a number
no alias holds: the commits since the base branch, `HEAD~1` by default, replace their existing entries in the
commit list (matched by subject) and new ones are appended; entries outside the range are kept and the list is
renumbered.

With `--per-commit`, `update` runs once per commit instead of once for the whole range, the way `review` does after
creating the changelist. It checks out each commit in turn, oldest first, runs the update on it, and returns to
where you started. With `--shelve`, every commit gets a shelf of its own and so shows up as its own patch in the
Swarm review.

- **With a record,** plain `update --per-commit --shelve` runs on exactly the commits the changelist doesn't include
  yet. `-b` is refused there, as it would shelve included commits again.
- **Without one,** pass the range: `update -b HEAD~2 --per-commit --shelve` updates the changelist with the last two
  commits, one at a time. The base is resolved once, before the first step, so a relative base like `HEAD~2` means
  the same commit for every step.

`-m`/`-F` is applied on the first step only. It needs a clean git workspace, also with `--no-edit`. If a step
fails, `update` returns to where you started and names the step and commit that failed. The error ends with the
command that resumes after the last commit that worked, so earlier commits get no second shelf; with a record,
running the same command again does the same.

```sh
git p4son update [changelist] [-m <message> | -F <file>] [--base-branch BASE_BRANCH] [--dry-run]
                 [--no-commit-list] [--no-edit] [--shelve] [--per-commit]
```

**Arguments:**
- `changelist` (optional): Changelist number or named alias to update. Defaults to the current branch name.

**Options:**
- `-m, --message MESSAGE`: Replace the changelist description message above the commit list. The commit list is
  kept and updated as usual, and so is a Swarm `#review` keyword on a line of its own at the end of the description
- `-F, --file FILE`: Like `-m`, but read the message from a file. The message may span multiple lines
- `-b, --base-branch BASE_BRANCH`: Where the commits to fold in start, for the commit list and the files to open.
  Defaults to after the last commit the changelist includes, when its alias records that, else to `HEAD~1`
- `-n, --dry-run`: Pretend and print what would be done, but do not execute. Ends with `Dry run, nothing changed`
- `--no-commit-list`: Leave the enumerated commit list as it is. Without `-m` or `-F` the description is not
  touched at all
- `--no-edit`: Skip opening changed files for edit in Perforce. With a record, the new commits are then neither
  appended nor counted, so the next update that opens files includes them
- `--shelve`: Re-shelve the changelist after updating
- `--per-commit`: Update the changelist once per commit it doesn't include yet, or since the base branch without
  a record, oldest first, with each commit checked out in turn. With `--shelve`, steps sleep 5 seconds between them
  to give Perforce/Swarm time to process each shelf. `--dry-run` prints the steps, one per commit

**Examples:**
```sh
git p4son update              # fold the new commits into the changelist for the current branch
git p4son update --shelve     # update and re-shelve
git p4son update 12345
git p4son update myalias -b main   # open every file of the change again
git p4son update -F description.txt   # replace the message, e.g. after review feedback
git p4son update --per-commit --shelve   # one shelf for each commit the changelist doesn't include yet
git p4son update 12345 -b HEAD~2 --per-commit --shelve   # no record: the last two commits
```

### Keeping the changelist in sync with git

`new` and `update` open every file changed since the base branch in the changelist. A file can later stop being
part of the change, for example when a later commit undoes an edit, or a file is added in one commit and deleted
in the next. Such files would otherwise stay opened as no-ops, so after opening files both commands revert them:

- Files opened for edit that are unchanged compared to the depot are reverted with `p4 revert -a`. Changed files
  stay opened.
- Files opened for add that are missing from disk are reverted.
- Files opened for delete that git still has (deleted in one commit, restored in a later one) are reverted and
  restored from git.

Only files git-p4son has authority over are touched: files tracked by git, and files opened for add that are
missing from disk. Anything else in the changelist, such as binaries that only exist in Perforce and were opened
by hand, is left alone even when unchanged. Files opened with other actions (`move/add`, `integrate`, ...) are
also left alone. This step is skipped with `--no-edit`.

Shelving (`--shelve`, `--review`) replaces the whole shelf with the changelist's opened files using
`p4 shelve -r -a leaveunchanged`, so the shelf never holds stale files, and unchanged files that are still opened
are left out of it.

### Review Command

Create a Swarm review with one shelf per commit. `review` checks out the first commit since the base branch and runs
`git p4son new --review` on it, then checks out each later commit and runs `git p4son update --shelve`, and finally
returns to where you started. Every step but the last sleeps 5 seconds to give Perforce/Swarm time to process the
shelf. The commits are reviewed where they are: to review them on top of the latest `main`, rebase first. Like
`new`, it needs a clean git workspace, untracked files included, and checks it even with `--dry-run`.

```sh
git p4son review -b BASE_BRANCH [alias] [-m <message> | -F <file>] [--force] [--dry-run] [--no-commit-list]
```

**Arguments:**
- `alias` (optional): Alias name for the new changelist. Defaults to the current branch name.

**Options:**
- `-m, --message MESSAGE`: Changelist description message. Defaults to the HEAD commit subject
- `-F, --file FILE`: Read the changelist description message from a file instead of `-m`. The message may span
  multiple lines
- `-b, --base-branch BASE_BRANCH`: Required. The base the change starts from, to find commits since: a branch or
  bookmark, or a commit exactly one points at, which the alias records as for [`new`](#new-command)
- `-f, --force`: Overwrite an existing alias file
- `-n, --dry-run`: Print the steps, one per commit, without running them. Ends with `Dry run, nothing changed`
- `--no-commit-list`: Leave the enumerated commit list out of the changelist description. Passed on to `new` and
  each `update` step

A dry run prints the plan like this, with the base resolved to the commit every step uses; the message, which only
the first step gets, is printed below it:

```
abc1234 First commit: git p4son new my-feature --review -b 5f1d0e9c2a7b4e8d9c0b1a2f3e4d5c6b7a8f9e0d
def5678 Second commit: git p4son update my-feature --shelve -b 5f1d0e9c2a7b4e8d9c0b1a2f3e4d5c6b7a8f9e0d
ghi9012 Third commit: git p4son update my-feature --shelve -b 5f1d0e9c2a7b4e8d9c0b1a2f3e4d5c6b7a8f9e0d
```

If a step fails, `review` returns to where you started and names the step and commit that failed. Once the first
step has created the changelist, the error also says how to finish: after fixing the problem, run the
`git p4son update my-feature --shelve --per-commit -b <commit>` it prints, which resumes after the last commit that
worked.

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

### List-Changes Command

List commit subjects since a base branch in chronological order (oldest first):

```sh
git p4son list-changes [--base-branch BASE_BRANCH]
```

**Options:**
- `-b, --base-branch BASE_BRANCH`: Base branch to compare against. Default is `HEAD~1`.

**Examples:**
```sh
git p4son list-changes
git p4son list-changes --base-branch main
```

This command is useful for generating changelist descriptions by listing all commit messages since the base branch,
numbered sequentially.

### Alias Command

Manage changelist aliases, stored one per file as `.git-p4son/aliases/<alias>.toml` (`changelist = 12345`, and
with the jj backend `change = "<change ID>"`, the [jj change](#jujutsu-jj) it was made for). An alias that `new` or
`review` saved also records the changelist's base and the commits since it it includes, which
[`update`](#update-command) goes on from: `base`, `base_commit` and `included`, a list of the commits' identities.
Aliases saved by older versions in `.git-p4son/changelists/` keep working, and move to `.git-p4son/aliases/` when
written, or all at once with `alias clean`.

#### alias list

List all aliases and their changelist numbers:

```sh
git p4son alias list
```

**Examples:**
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

#### alias delete

Delete a changelist alias, or all of them:

```sh
git p4son alias delete [alias | --all]
```

**Arguments:**
- `alias` (optional): Alias name to delete. Defaults to the current branch name.

**Options:**
- `--all`: Delete all aliases without prompting. Unlike `alias clean`, it does not ask first, which suits scripts
  and AI agents

**Examples:**
```sh
git p4son alias delete              # delete alias for current branch
git p4son alias delete myfeature
git p4son alias delete --all
```

#### alias clean

Clean up changelist aliases:

```sh
git p4son alias clean
```

It first moves any aliases still in `.git-p4son/changelists/` to `.git-p4son/aliases/`, then lists all aliases and
asks how to proceed:
- `a` (all): Delete all aliases
- `i` (interactive): Review each alias one by one
- `q` (quit): Exit without deleting anything

In interactive mode, each alias is displayed in turn with a prompt:
- `y` (yes): Delete this alias
- `n` (no): Keep this alias
- `a` (all): Delete this and all remaining aliases (handy for keeping the first few and sweeping the rest)
- `q` (quit): Stop and keep remaining aliases

**Examples:**
```sh
git p4son alias clean
```

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

## Hooks

Hooks work similarly to git hooks. They are executables (unix)/scripts (windows) that you place in
the `.git-p4son/hooks/[hook-name]/` directory. These scripts will be called to add or modify behavior
at defined times during git-p4son operations. Scripts get run in filename (lexicographically) sorted
order.

The hook process CWD is the directory where git-p4son was invoked, and `GIT_P4SON_REPO_ROOT_DIR` is
set to the git-p4son repository root. `GIT_P4SON_BACKEND` is `git` or `jj`, the [backend](#choosing-the-backend)
the command runs with.

On Windows, `.ps1`, `.nu`, `.sh`, and `.py` files are run with
`powershell.exe -NoProfile -ExecutionPolicy Bypass -File`, `nu.exe`, `bash.exe`, and `python.exe` respectively.
Customize or add Windows script associations in `.git-p4son/config.toml`:

```toml
[hooks.extension-associations]
".ps1" = ["pwsh.exe", "-NoProfile", "-File"]
".py" = "python.exe"
```

Non-executable or unrecognized hook files, and invalid association entries, are skipped with a warning.

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
- An alias records one change, and a change one alias: `new` refuses a change another alias records, naming it, and
  `--force` moves the record to the new alias. `alias new` records no change. `alias list` shows each recorded change,
  marked `(gone)` once jj no longer has it.
- The default message is the first line of the change's description. Use `jj describe`, `-m` or `-F`.
- `-b/--base-branch` takes a jj revision that resolves to one change, such as a bookmark, change ID or commit ID.
  Where it has a default, it's the change's parent rather than git's `HEAD~1`. `new` and `review` record it as a
  bookmark, so it has to name one, or resolve to a commit exactly one points at.
- `new` and `update` use jj's snapshot of the working copy, check that it did not change while git-p4son took the
  lock, and refuse files jj left untracked. You do not need a separate committed change before opening files in
  Perforce. Before a Perforce revert overwrites a file, git-p4son saves the working file and puts it back
  afterwards, keeping its bytes, line endings and permissions instead of taking git's index version.
- `init` holds jj's working-copy lock while it sets up the git repository. It also offers to colocate jj and to
  add the `jj p4son` alias, see [Init Command](#init-command).

For example:

```sh
jj git init --colocate       # Add jj to an existing git-p4son repository, or answer yes in init
git p4son sync               # From any change: syncs main, then rebases your changes onto it

jj new main -m "Fix login"
# Edit files
jj bookmark create fix-login
git p4son new -b main --review
# Edit more files
git p4son update -b main --shelve
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

### Locking, and the git workflow

git-p4son holds jj's working-copy lock while commands run, including sync hooks and temporary Perforce reverts,
so other jj processes cannot snapshot files halfway through. A busy lock stops the command. Queries during the
command use a pinned jj operation without snapshotting.

jj's first snapshot and final reconciliation happen outside the lock, and so do sync's rebase and return, so
another jj command can import the finished git changes before git-p4son does: this is not one atomic jj
transaction. `--verbose` logs the jj operation git-p4son pinned at the start. The lock does not keep out
editors, git commands or other jj workspaces. If a command fails, look at the workspace before continuing: the
lock is released, but Perforce operations that completed are not undone.

Hooks must not run ordinary jj commands, which would wait for the lock their caller holds. Read-only queries such
as `jj --at-op=@ log` avoid snapshotting and the git import. A hook that runs git-p4son itself needs
`--backend git`, which reuses the caller's lock.

`review` and `update --per-commit` run once per commit since the base, up to the jj change, as they do in git.
Under jj's lock, each step puts its commit's files on disk with git, and the working change's files go back at the
end, also when a step fails, so jj sees no change. No jj command runs, and untracked and ignored files are left
alone. They refuse to start if files changed after jj's snapshot, as the checkouts would overwrite them:

```sh
git p4son review -b main
git p4son update --per-commit --shelve -b main
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

