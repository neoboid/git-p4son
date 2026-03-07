#!/bin/sh
# Set up git hooks by pointing core.hooksPath to the repo's hooks/ directory.
set -e

repo_root="$(cd "$(dirname "$0")/.." && pwd)"
git -C "$repo_root" config core.hooksPath "$repo_root/hooks"
echo "Git hooks configured from $repo_root/hooks"
