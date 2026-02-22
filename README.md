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

## Installation

```sh
pip install git-p4son
```

Requires Python 3.10 or later.

## Development

To install from source in editable mode:

```sh
git clone https://github.com/neoboid/git-p4son.git
cd git-p4son
pip install -e .
```

The `-e` flag installs the package in "editable" mode, meaning changes to the code are immediately available
without reinstalling. This is also handy if you want to auto-update git-p4son whenever you pull from GitHub.

git-p4son only uses Python standard library modules — no additional packages are required.

## Setup

These steps set up git-p4son in an existing Perforce workspace. You only need to do this once.

1. **Enable clobber on your Perforce workspace.** This allows `p4 sync` to overwrite writable files, which is
   necessary because git removes the read-only flag on files it touches. Edit the workspace in P4V to set the
   clobber flag, or run `p4 client` and change `noclobber` to `clobber` in the Options line.

2. **Sync your workspace to a known changelist.** Pick a changelist to use as the starting point for your git
   history:
   ```sh
   p4 sync //...@12345
   ```

3. **Initialize the git repo with git-p4son.** This can be anywhere inside your Perforce workspace — it doesn't
   have to be at the root:
   ```sh
   cd /path/to/your/workspace    # or a subdirectory of it
   git p4son init
   ```
   This checks preconditions (P4 workspace, clobber flag), runs `git init`, sets up `.gitignore` (copying from
   `.p4ignore` if available), and creates an initial commit.

4. **Review `.gitignore`.** Edit the file to ensure build artifacts and other unwanted files are excluded before
   adding workspace files.

5. **Run the first sync** to add all workspace files with a tracked commit:
   ```sh
   git p4son sync latest
   ```

From here, branch off `main` for local development. See the [Usage Example](#usage-example) below for a typical
workflow.

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

This command checks preconditions (Perforce workspace, clobber flag), runs `git init`, sets up `.gitignore`,
and creates an initial commit. The `.gitignore` is set up using this priority:
- If `.gitignore` already exists, it is left as is
- If `.p4ignore` exists, it is copied to `.gitignore` as a starting point
- Otherwise, an empty `.gitignore` is created

### Sync Command

Sync local git repository with a Perforce workspace:

```sh
git p4son sync <changelist> [--force]
```

**Arguments:**
- `changelist`: Changelist number, named alias, or special keywords:
  - `latest`: Sync to the latest changelist affecting the workspace
  - `last-synced`: Re-sync the last synced changelist

**Options:**
- `-f, --force`: Force sync encountered writable files and allow syncing to older changelists.

**Examples:**
```sh
git p4son sync 12345
git p4son sync latest
git p4son sync last-synced
git p4son sync 12345 --force
```

### New Command

Create a new Perforce changelist and add changed files to it. Description will contain an enumerated list of git commits since the base branch.
Optionally creates a Swarm review.

```sh
git p4son new -m <message> [--base-branch BASE_BRANCH] [alias] [--force] [--dry-run] [--no-edit] [--shelve] [--review]
```

**Arguments:**
- `alias`: Optional alias name to save the new changelist number under

**Options:**
- `-m, --message MESSAGE`: Changelist description message (required)
- `-b, --base-branch BASE_BRANCH`: Base branch for enumerating commits and finding changed files. Default is `HEAD~1`
- `-f, --force`: Overwrite an existing alias file
- `-n, --dry-run`: Pretend and print what would be done, but do not execute
- `--no-edit`: Skip opening changed files for edit in Perforce
- `--shelve`: Shelve the changelist after creating it
- `--review`: Add `#review` keyword and shelve to create a Swarm review
- `-s, --sleep SECONDS`: Sleep for the specified number of seconds after the command is done

**Examples:**
```sh
git p4son new -m "Fix login bug"
git p4son new -m "Add feature" -b main
git p4son new -m "Fix bug" myalias
git p4son new -m "Fix bug" --no-edit
git p4son new -m "New feature" --review -b main myalias
```

### Update Command

Update an existing Perforce changelist description by replacing the enumerated commit list with the current commits since the base branch. By default also opens changed files for edit.

```sh
git p4son update <changelist> [--base-branch BASE_BRANCH] [--dry-run] [--no-edit] [--shelve]
```

**Arguments:**
- `changelist`: Changelist number or named alias to update

**Options:**
- `-b, --base-branch BASE_BRANCH`: Base branch for enumerating commits and finding changed files. Default is `HEAD~1`
- `-n, --dry-run`: Pretend and print what would be done, but do not execute
- `--no-edit`: Skip opening changed files for edit in Perforce
- `--shelve`: Re-shelve the changelist after updating
- `-s, --sleep SECONDS`: Sleep for the specified number of seconds after the command is done

**Examples:**
```sh
git p4son update 12345
git p4son update myalias -b main
git p4son update myalias --shelve
git p4son update 12345 --no-edit
```

### Review Command

Automate the interactive rebase workflow for creating Swarm reviews. This command generates a rebase todo with `exec` lines that run `git p4son new --review` on the first commit and `git p4son update --shelve` on each subsequent commit, then opens it in your editor for review before executing.

```sh
git p4son review <alias> -m <message> [--base-branch BASE_BRANCH] [--force] [--dry-run]
```

**Arguments:**
- `alias`: Alias name for the new changelist

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
# Review all commits since main
git p4son review my-feature -m "Add my feature" -b main

# Review just the last commit (default base branch)
git p4son review my-feature -m "Fix bug"

# Preview the generated todo without executing
git p4son review my-feature -m "Add my feature" -b main --dry-run
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

#### alias set

Save a changelist number under a named alias:

```sh
git p4son alias set <changelist> <alias> [--force]
```

**Arguments:**
- `changelist`: Changelist number to save
- `alias`: Alias name to save the changelist number under

**Options:**
- `-f, --force`: Overwrite an existing alias file

**Examples:**
```sh
git p4son alias set 12345 myfeature
git p4son alias set 67890 myfeature -f
```

#### alias delete

Delete a changelist alias:

```sh
git p4son alias delete <alias>
```

**Arguments:**
- `alias`: Alias name to delete

**Examples:**
```sh
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
- `shell`: Shell to print completion script path for (`zsh` or `powershell`)

**Options:**
- `-d, --dirname`: Print the directory containing the completion script instead of the full file path

See [Shell Completions](#shell-completions) below for installation instructions.

### The `branch` keyword

Several commands accept a special `branch` keyword that resolves to an alias name derived from the current git
branch. This lets you avoid typing the alias name manually when it matches your branch.

The keyword works anywhere an alias or changelist argument is accepted:
- `git p4son new branch -m "Fix bug"` — creates a changelist and saves the alias under the current branch name
- `git p4son update branch --shelve` — updates the changelist associated with the current branch
- `git p4son review branch -m "Feature" -b main` — creates a review using the branch name as alias
- `git p4son alias set 12345 branch` — saves a changelist number under the current branch name

The keyword cannot be used on the `main` branch or in a detached HEAD state.

## Shell Completions

Tab completion is available for both zsh and PowerShell, including commands, flags, and dynamic alias names.

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

Both enable completion for `git p4son <TAB>` and `git-p4son <TAB>`.

## Usage Example

Here's a typical workflow using git-p4son:

```sh
# Sync main with latest changes from perforce
git checkout main
git p4son sync latest

# Start work on a new feature
git checkout -b my-fancy-feature

# Change some code
git add .
git commit -m "Feature part1"

# Sync to the latest changelist affecting the workspace
git checkout main
git p4son sync latest

# Rebase your changes on main
git checkout my-fancy-feature
git rebase main

# Change even more code
git add .
git commit -m "Feature part2"

# Create a Swarm review with all commits since main in one go.
# This opens an interactive rebase with pre-filled exec lines updating changelist
# with git-p4son after each picked changelist.
#
# "branch" is a special keyword that gets resolved to current git branch.
# in this case the review is put in a new changelist, and an alias called "my-fancy-feature" is
# set up for this changelist number so that you can refer to this CL with the alias instead of number
# in follow up commands.
git p4son review branch -m "My fancy feature" -b main

# After review feedback, make more changes
git add .
git commit -m "Address review feedback"

# Update the changelist with latest commit, re-open files, and re-shelve
# We could have used "branch" here instead of spelling out the alias by name
git p4son update my-fancy-feature --shelve

# After approval, submit in p4v

# Sync to the latest changelist from perforce
git checkout main
git p4son sync latest

# Force remove old branch as you don't need it anymore
git branch -D my-fancy-feature

# Remove changelist alias
git p4son alias delete my-fancy-feature

# Start working on the next feature
git checkout -b my-next-fancy-feature
```
