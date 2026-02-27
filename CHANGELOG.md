# Changelog

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
