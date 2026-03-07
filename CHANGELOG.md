# Changelog

## 0.2.3

- Enable syncing just a sub-directory of your perforce workspace
- Add pre-commit hook to enforce autopep8 formatting
- Add pre-push hook that runs tests before pushing
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
