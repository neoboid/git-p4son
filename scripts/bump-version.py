#!/usr/bin/env python3
"""Bump the version of git-p4son.

Two-step workflow:
  1. bump-version.py [patch|minor|major]: bump the version files and move the
     entries under "## Unreleased" in CHANGELOG.md under the new version
  2. (edit CHANGELOG.md if desired)
  3. bump-version.py --finalize: commit, tag, push and create the GitHub
     release
"""

import argparse
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / 'pyproject.toml'
INIT_PY = ROOT / 'git_p4son' / '__init__.py'
CHANGELOG = ROOT / 'CHANGELOG.md'

VERSION_RE = re.compile(r'(\d+)\.(\d+)\.(\d+)')


def read_version(path, pattern):
    """Read a version string from a file matching the given pattern."""
    text = path.read_text()
    m = re.search(pattern, text)
    if not m:
        print(f'Error: could not find version in {path}', file=sys.stderr)
        sys.exit(1)
    return m.group(1)


def parse_version(version_str):
    """Parse a version string into (major, minor, patch)."""
    m = VERSION_RE.fullmatch(version_str)
    if not m:
        print(f'Error: invalid version format: {version_str}', file=sys.stderr)
        sys.exit(1)
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def bump(version, part):
    """Bump a (major, minor, patch) tuple by the given part."""
    major, minor, patch = version
    if part == 'major':
        return major + 1, 0, 0
    elif part == 'minor':
        return major, minor + 1, 0
    else:
        return major, minor, patch + 1


def replace_in_file(path, old, new):
    """Replace a string in a file."""
    text = path.read_text()
    if old not in text:
        print(f'Error: could not find "{old}" in {path}', file=sys.stderr)
        sys.exit(1)
    path.write_text(text.replace(old, new, 1))


def run(cmd, input=None):
    """Run a command and exit on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True, input=input)
    if result.returncode != 0:
        print(f'Error running: {" ".join(cmd)}', file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(1)
    return result


UNRELEASED_RE = re.compile(r'^## Unreleased\n(.*?)(?=^## |\Z)',
                           re.MULTILINE | re.DOTALL)


def unreleased_entries():
    """Return the entries under "## Unreleased" in CHANGELOG.md."""
    m = UNRELEASED_RE.search(CHANGELOG.read_text())
    if not m or not m.group(1).strip():
        print(f'Error: no entries under "## Unreleased" in {CHANGELOG}. '
              'Describe the changes there first.', file=sys.stderr)
        sys.exit(1)
    return m.group(1).strip().splitlines()


def update_changelog(version):
    """Move the Unreleased entries in CHANGELOG.md under a new heading."""
    text = re.sub(r'^## Unreleased\n+', f'## Unreleased\n\n## {version}\n\n',
                  CHANGELOG.read_text(), count=1, flags=re.MULTILINE)
    CHANGELOG.write_text(text)


def changelog_section(version):
    """Return the CHANGELOG.md section for a version, without its heading."""
    text = CHANGELOG.read_text()
    m = re.search(rf'^## {re.escape(version)}\n(.*?)(?=^## |\Z)', text,
                  re.MULTILINE | re.DOTALL)
    if not m or not m.group(1).strip():
        print(f'Error: no entries for {version} in {CHANGELOG}',
              file=sys.stderr)
        sys.exit(1)
    return m.group(1).strip() + '\n'


def prepare(args):
    """Prepare a release: bump version files and generate changelog."""
    # Read current version from pyproject.toml
    pyproject_version = read_version(PYPROJECT, r'version\s*=\s*"([^"]+)"')
    init_version = read_version(INIT_PY, r'__version__\s*=\s*"([^"]+)"')

    if pyproject_version != init_version:
        print(
            f'Error: version mismatch, pyproject.toml has '
            f'{pyproject_version} and __init__.py has {init_version}',
            file=sys.stderr)
        sys.exit(1)

    old = parse_version(pyproject_version)
    new = bump(old, args.part)
    old_str = pyproject_version
    new_str = f'{new[0]}.{new[1]}.{new[2]}'
    tag = f'v{new_str}'

    # Safety: working tree must be clean
    status = run(['git', 'status', '--porcelain'])
    if status.stdout.strip():
        print('Error: working tree is not clean. Commit or stash changes '
              'first.', file=sys.stderr)
        sys.exit(1)

    # Safety: tag must not exist
    existing_tags = run(['git', 'tag', '--list', tag])
    if existing_tags.stdout.strip():
        print(f'Error: tag {tag} already exists.', file=sys.stderr)
        sys.exit(1)

    # Safety: the release must be described in CHANGELOG.md
    notes = unreleased_entries()

    # Safety: all tests must pass
    print('Running tests...')
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', 'tests/', '-q'],
        cwd=ROOT)
    if result.returncode != 0:
        print('Error: tests failed. Fix them before releasing.',
              file=sys.stderr)
        sys.exit(1)

    # Update files
    replace_in_file(
        PYPROJECT, f'version = "{old_str}"', f'version = "{new_str}"')
    replace_in_file(
        INIT_PY, f'__version__ = "{old_str}"', f'__version__ = "{new_str}"')
    update_changelog(new_str)

    print(f'{old_str} -> {new_str}')
    print()
    print('Release notes:')
    for line in notes:
        print(f'  {line}')
    print()
    print('Review CHANGELOG.md, then run:')
    print('  python scripts/bump-version.py --finalize')


def finalize(args):
    """Commit, tag, push and create the GitHub release."""
    version = read_version(PYPROJECT, r'version\s*=\s*"([^"]+)"')
    tag = f'v{version}'

    # Safety: tag must not exist (confirms prepare ran but finalize hasn't)
    existing_tags = run(['git', 'tag', '--list', tag])
    if existing_tags.stdout.strip():
        print(f'Error: tag {tag} already exists.', file=sys.stderr)
        sys.exit(1)

    # Check everything the release needs before committing, so a failure
    # cannot leave a half-done release behind
    branch = run(['git', 'rev-parse', '--abbrev-ref', 'HEAD']).stdout.strip()
    if branch == 'HEAD':
        print('Error: detached HEAD. Check out the branch to release from.',
              file=sys.stderr)
        sys.exit(1)
    if not shutil.which('gh'):
        print('Error: the GitHub CLI (gh) is needed to create the release.',
              file=sys.stderr)
        sys.exit(1)
    run(['gh', 'auth', 'status'])
    notes = changelog_section(version)

    # Commit and tag
    commit_msg = f'Release git-p4son v{version}'
    run(['git', 'add', str(PYPROJECT), str(INIT_PY), str(CHANGELOG)])
    run(['git', 'commit', '-m', commit_msg])
    run(['git', 'tag', tag])
    print(f'Committed: {commit_msg}')
    print(f'Tagged: {tag}')

    # Push and release. Pushing the tag starts the PyPI publish workflow.
    # Each step is (message when done, command, stdin, command to show if it
    # has to be run by hand).
    release_cmd = ['gh', 'release', 'create', tag, '--verify-tag', '--latest',
                   '--title', f'git-p4son {tag}', '--notes-file']
    steps = [
        (f'Pushed {branch}', ['git', 'push', 'origin', branch], None, None),
        (f'Pushed {tag}', ['git', 'push', 'origin', tag], None, None),
        (f'Created GitHub release {tag}', release_cmd + ['-'], notes,
         f'{shlex.join(release_cmd)} <the {version} section of '
         'CHANGELOG.md>'),
    ]
    for i, (done, cmd, stdin, _) in enumerate(steps):
        result = subprocess.run(cmd, capture_output=True, text=True,
                                input=stdin)
        if result.returncode != 0:
            print(f'Error running: {shlex.join(cmd)}', file=sys.stderr)
            if result.stderr:
                print(result.stderr, file=sys.stderr)
            print('Finish the release by hand:', file=sys.stderr)
            for _, remaining, _, shown in steps[i:]:
                print(f'  {shown or shlex.join(remaining)}', file=sys.stderr)
            sys.exit(1)
        print(done)


def main():
    parser = argparse.ArgumentParser(description='Bump the git-p4son version.')
    parser.add_argument(
        'part',
        nargs='?',
        default='patch',
        choices=['major', 'minor', 'patch'],
        help='Which part to bump (default: patch)'
    )
    parser.add_argument(
        '--finalize',
        action='store_true',
        help='Commit and tag the prepared release'
    )
    args = parser.parse_args()

    if args.finalize:
        finalize(args)
    else:
        prepare(args)


if __name__ == '__main__':
    main()
