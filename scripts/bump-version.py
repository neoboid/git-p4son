#!/usr/bin/env python3
"""Bump the version of git-p4son.

Updates pyproject.toml and git_p4son/__init__.py, commits, and tags.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / 'pyproject.toml'
INIT_PY = ROOT / 'git_p4son' / '__init__.py'

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


def run(cmd):
    """Run a command and exit on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f'Error running: {" ".join(cmd)}', file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(1)
    return result


def main():
    parser = argparse.ArgumentParser(description='Bump the git-p4son version.')
    parser.add_argument(
        'part',
        nargs='?',
        default='patch',
        choices=['major', 'minor', 'patch'],
        help='Which part to bump (default: patch)'
    )
    args = parser.parse_args()

    # Read current version from pyproject.toml
    pyproject_version = read_version(PYPROJECT, r'version\s*=\s*"([^"]+)"')
    init_version = read_version(INIT_PY, r'__version__\s*=\s*"([^"]+)"')

    if pyproject_version != init_version:
        print(
            f'Error: version mismatch — pyproject.toml has {pyproject_version}, '
            f'__init__.py has {init_version}',
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
        print('Error: working tree is not clean. Commit or stash changes first.',
              file=sys.stderr)
        sys.exit(1)

    # Safety: tag must not exist
    existing_tags = run(['git', 'tag', '--list', tag])
    if existing_tags.stdout.strip():
        print(f'Error: tag {tag} already exists.', file=sys.stderr)
        sys.exit(1)

    # Update files
    replace_in_file(PYPROJECT, f'version = "{old_str}"', f'version = "{new_str}"')
    replace_in_file(INIT_PY, f'__version__ = "{old_str}"', f'__version__ = "{new_str}"')

    # Commit and tag
    commit_msg = f'Release git-p4son v{new_str}'
    run(['git', 'add', str(PYPROJECT), str(INIT_PY)])
    run(['git', 'commit', '-m', commit_msg])
    run(['git', 'tag', tag])

    print(f'{old_str} -> {new_str}')
    print(f'Committed: {commit_msg}')
    print(f'Tagged: {tag}')
    print()
    print(f'To publish, push the commit and tag:')
    print(f'  git push && git push origin {tag}')


if __name__ == '__main__':
    main()
