# git-p4son

A tool for managing a local git repository within a Perforce workspace.

The idea is to have a `main` branch that is kept in sync with the Perforce depot.
From `main` you branch out into feature branches, where you do local
changes and rebase on `main` whenever it is updated.

This is a bit cumbersome to do manually, but this package provides commands
that help out with the repetitive and error prone stuff.

## Installation

Currently, git-p4son must be installed from source. Clone the repository and install:

```sh
git clone https://github.com/derwiath/git-p4son.git
cd git-p4son
pip install .
```

Or install in development mode:

```sh
git clone https://github.com/derwiath/git-p4son.git
cd git-p4son
pip install -e .
```

## Development

To contribute to git-p4son or modify it for your needs, you can install it in development mode:

```sh
git clone https://github.com/derwiath/git-p4son.git
cd git-p4son
pip install -e .
```

The `-e` flag installs the package in "editable" mode. Which means that changes
to the code are immediately available and `git p4son` can be tested right
away without reinstalling.

### Development Requirements

git-p4son only uses Python standard library modules, no additional packages are required.

## Setup

### Perforce workspace
* Set clobber flag on your perforce workspace.
* Sync workspace to a specified changelist
```sh
p4 sync //...@123
```
  Take note of the changelist number.

### Local git repo
* Initialize a local git repo:
```sh
git init
```
  It does not have to be in the root of your perforce workspace, you may choose to only
  keep a part of it in your local git repo.
* Add a `.gitignore` file and commit.
  Ideally your ignore file should ignore the same files that is ignored
  by perforce.
* Add all files and commit
```sh
git add .
git commit -m "Initial commit for CL 123"
```

## Usage

git-p4son provides five commands: `sync`, `new`, `update`, `list-changes`, and `alias`.

To see help for any command, use `-h`:

```sh
git p4son -h
git p4son sync -h
```

**Note:** When invoking via `git p4son`, the `--help` flag is intercepted by git (to look for man pages). Use `-h` instead, or `git p4son -- --help` to force it through. Alternatively, call the executable directly: `git-p4son --help`.

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

Create a new Perforce changelist with a description and enumerated git commits since the base branch. By default also opens changed files for edit in the changelist. Optionally creates a Swarm review.

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

**Examples:**
```sh
git p4son update 12345
git p4son update myalias -b main
git p4son update myalias --shelve
git p4son update 12345 --no-edit
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

## Usage Example

Here's a typical workflow using git-p4son:

```sh
# Sync main with new changes from perforce, CL 124
git checkout main
git p4son sync 124

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

# List all commit messages since main branch (useful for changelist description)
git p4son list-changes --base-branch main

# Create a new changelist, open files for edit, and create a Swarm review
git p4son new -m "My fancy feature" --review -b main myfeature

# After review feedback, make more changes
git add .
git commit -m "Address review feedback"

# Update the changelist, re-open files, and re-shelve
git p4son update myfeature --shelve -b main

# After approval, submit in p4v

# Sync to the latest changelist from perforce
git checkout main
git p4son sync latest

# Remove old branch as you don't need it anymore
git branch -D my-fancy-feature

# Start working on the next feature
git checkout -b my-next-fancy-feature
```
