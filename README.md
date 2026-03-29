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
# This opens an interactive rebase with pre-filled exec lines updating
# the changelist with git-p4son after each picked commit.
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

For tab completion (zsh and PowerShell), see [Shell Completions](#shell-completions).

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

### Git Hooks

The repository includes git hooks in the `hooks/` directory:

- **pre-commit** - verifies that staged Python files are formatted with `autopep8`. Install it with
  `pip install autopep8`.
- **pre-push** - runs the test suite and blocks the push if any test fails.

To activate the hooks, run:

```sh
scripts/setup-hooks.sh
```

This configures `core.hooksPath` so git uses the hooks from the repository. You only need to run this once after
cloning.

## Setup

These steps set up git-p4son in an existing Perforce workspace. You only need to do this once.

1. **Set a git editor** if you don't have one already. The `review` command opens an interactive rebase in your
   editor. If you haven't configured one, set it with:
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
that when you switch back to `main` for a sync, Perforce may report "Can't clobber writable file" errors. git-p4son
handles these automatically by classifying each writable file:

**writable-unchanged** - The file on disk is identical to what Perforce expects (only the read-only flag was removed
by git). This is the common case after switching branches. git-p4son force-syncs these files automatically using MD5
checksum comparison.

**writable-changed** - The file on disk differs from what Perforce has. This happens when you manually edit a file
on `main` and commit it to git without submitting to Perforce. git-p4son force-syncs the file, includes it in the
sync commit (so the commit reflects pure Perforce state), then three-way merges your local changes on top of the
new Perforce content - similar to a `git rebase`. If the merge is clean, the result appears as an unstaged change.
If there are conflicts, standard conflict markers are inserted for you to resolve.

**binary files** - Binary files that have local changes cannot be three-way merged. git-p4son restores your local
version to disk after the sync commit so it shows up in `git status` for you to handle manually.

**added locally, deleted upstream** - A file you added to git was deleted in Perforce. git-p4son restores your
version to disk and warns you.

**deleted locally, added upstream** - A file you deleted from git was modified in Perforce. The Perforce version is
kept in the sync commit. You can delete it again if you want.

**ignored** - Files ignored by `.gitignore` are skipped with a warning. These are outside git's domain and should
be managed manually or via Perforce directly.

After syncing, git-p4son prints a summary of how each writable file was handled, and instructs you to review and
commit any files that need attention.

## Usage

git-p4son provides eight commands: `init`, `sync`, `new`, `update`, `review`, `list-changes`, `alias`,
and `completion`.

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

This command checks preconditions (Perforce workspace), configures the depot root, runs `git init`,
sets up `.gitignore`, and creates an initial commit.

The depot root determines which part of the Perforce workspace git-p4son syncs. You can choose to sync the entire
workspace or just the directory's subtree where the git root is placed. The selection is saved in
`.git-p4son/config.toml` and used by all subsequent commands.

The `.gitignore` is set up using this priority:
- If `.gitignore` already exists, it is left as is
- If `.p4ignore` exists, it is copied to `.gitignore` as a starting point
- Otherwise, an empty `.gitignore` is created

### Sync Command

Sync local git repository with a Perforce workspace:

```sh
git p4son sync [changelist] [--force]
```

**Arguments:**
- `changelist` (optional): Changelist number, or `last-synced` to re-sync the last synced changelist. Omit to
  sync to the latest changelist affecting the workspace.

**Options:**
- `-f, --force`: Allow syncing to changelists older than the current one.

**Examples:**
```sh
git p4son sync              # sync to latest
git p4son sync 12345
git p4son sync last-synced
git p4son sync 12345 --force
```

### New Command

Create a new Perforce changelist and add changed files to it. Description will contain an enumerated list of git commits since the base branch.
Optionally creates a Swarm review.

```sh
git p4son new -m <message> [alias] [--base-branch BASE_BRANCH] [--force] [--dry-run] [--no-edit] [--no-alias]
                           [--shelve] [--review]
```

**Arguments:**
- `alias` (optional): Alias name to save the new changelist number under. Defaults to the current branch name.

**Options:**
- `-m, --message MESSAGE`: Changelist description message (required)
- `-b, --base-branch BASE_BRANCH`: Base branch for enumerating commits and finding changed files. Default is
  `HEAD~1`
- `-f, --force`: Overwrite an existing alias file
- `-n, --dry-run`: Pretend and print what would be done, but do not execute
- `--no-edit`: Skip opening changed files for edit in Perforce
- `--no-alias`: Skip saving a changelist alias
- `--shelve`: Shelve the changelist after creating it
- `--review`: Add `#review` keyword and shelve to create a Swarm review
- `-s, --sleep SECONDS`: Sleep for the specified number of seconds after the command is done

**Examples:**
```sh
git p4son new -m "Fix login bug"
git p4son new -m "Add feature" -b main
git p4son new -m "Fix bug" myalias
git p4son new -m "Fix bug" --no-alias
git p4son new -m "New feature" --review -b main
```

### Update Command

Update an existing Perforce changelist description by replacing the enumerated commit list with the current commits since the base branch. By default also opens changed files for edit.

```sh
git p4son update [changelist] [--base-branch BASE_BRANCH] [--dry-run] [--no-desc] [--no-edit] [--shelve]
```

**Arguments:**
- `changelist` (optional): Changelist number or named alias to update. Defaults to the current branch name.

**Options:**
- `-b, --base-branch BASE_BRANCH`: Base branch for enumerating commits and finding changed files. Default is
  `HEAD~1`
- `-n, --dry-run`: Pretend and print what would be done, but do not execute
- `--no-desc`: Skip updating the changelist description
- `--no-edit`: Skip opening changed files for edit in Perforce
- `--shelve`: Re-shelve the changelist after updating
- `-s, --sleep SECONDS`: Sleep for the specified number of seconds after the command is done

**Examples:**
```sh
git p4son update              # update changelist for current branch
git p4son update --shelve     # update and re-shelve
git p4son update 12345
git p4son update myalias -b main
```

### Review Command

Automate the interactive rebase workflow for creating Swarm reviews. This command generates a rebase todo with `exec` lines that run `git p4son new --review` on the first commit and `git p4son update --shelve` on each subsequent commit, then opens it in your editor for review before executing.

```sh
git p4son review [alias] -m <message> [--base-branch BASE_BRANCH] [--force] [--dry-run]
```

**Arguments:**
- `alias` (optional): Alias name for the new changelist. Defaults to the current branch name.

**Options:**
- `-m, --message MESSAGE`: Changelist description message (required)
- `-b, --base-branch BASE_BRANCH`: Base branch to rebase onto and find commits since. Default is `HEAD~1`
- `-f, --force`: Overwrite an existing alias file
- `-n, --dry-run`: Print the generated rebase todo without executing

When run, the command generates a todo like this and opens it in your editor:

```
pick abc1234 First commit
exec git p4son new my-feature --review -m 'My feature'
pick def5678 Second commit
exec git p4son update my-feature --shelve
pick ghi9012 Third commit
exec git p4son update my-feature --shelve
```

You can edit the todo before saving (e.g. reorder commits, remove lines), or abort by clearing the file — just like a normal `git rebase -i`. Each `exec` line automatically sleeps after shelving to give Perforce/Swarm time to process.

If the rebase fails mid-way, you can fix the issue and run `git rebase --continue` as usual.

**Examples:**
```sh
# Review all commits since main (alias defaults to branch name)
git p4son review -m "Add my feature" -b main

# Review just the last commit (default base branch)
git p4son review -m "Fix bug"

# Preview the generated todo without executing
git p4son review -m "Add my feature" -b main --dry-run
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

This command is useful for generating changelist descriptions by listing all commit messages since the base branch, numbered sequentially.

### Alias Command

Manage changelist aliases stored in `.git-p4son/changelists/`.

#### alias list

List all aliases and their changelist numbers:

```sh
git p4son alias list
```

**Examples:**
```sh
git p4son alias list
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

Delete a changelist alias:

```sh
git p4son alias delete [alias]
```

**Arguments:**
- `alias` (optional): Alias name to delete. Defaults to the current branch name.

**Examples:**
```sh
git p4son alias delete              # delete alias for current branch
git p4son alias delete myfeature
```

#### alias clean

Interactively review and delete changelist aliases:

```sh
git p4son alias clean
```

This command iterates through each alias, displays it, and prompts for action:
- `y` (yes): Delete this alias
- `n` (no): Keep this alias
- `a` (all): Delete this and all remaining aliases
- `q` (quit): Stop and keep remaining aliases

**Examples:**
```sh
git p4son alias clean
```

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

### The `branch` keyword

Most commands that accept an alias or changelist argument default to the current branch name. You can also pass the
`branch` keyword explicitly — it resolves to an alias name derived from the current git branch.

The keyword cannot be used in a detached HEAD state. Use `--no-alias` (on `new` and `review`) or supply an
explicit alias name instead.

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
