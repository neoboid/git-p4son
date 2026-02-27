#!/usr/bin/env python3
"""Bump the version of git-p4son.

Two-step workflow:
  1. bump-version.py [patch|minor|major]  — bump version files, generate changelog
  2. (edit CHANGELOG.md if desired)
  3. bump-version.py --finalize            — commit and tag
"""

import argparse
import re
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


def run(cmd):
    """Run a command and exit on failure."""
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f'Error running: {" ".join(cmd)}', file=sys.stderr)
        if result.stderr:
            print(result.stderr, file=sys.stderr)
        sys.exit(1)
    return result


def get_previous_tag():
    """Return the most recent version tag, or None if no tags exist."""
    result = subprocess.run(
        ['git', 'describe', '--tags', '--abbrev=0'],
        capture_output=True, text=True)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def get_release_notes(previous_tag):
    """Get commit subjects since previous_tag, excluding release commits."""
    if previous_tag:
        cmd = ['git', 'log', f'{previous_tag}..HEAD', '--format=%s']
    else:
        cmd = ['git', 'log', '--format=%s']
    result = run(cmd)
    subjects = result.stdout.strip().splitlines()
    return [s for s in subjects if not s.startswith('Release git-p4son v')]


def update_changelog(version, notes):
    """Prepend a new version section to CHANGELOG.md."""
    section = f'\n## {version}\n\n'
    for note in notes:
        section += f'- {note}\n'

    if CHANGELOG.exists():
        text = CHANGELOG.read_text()
        # Insert after the "# Changelog" header line
        header_end = text.index('\n') + 1
        text = text[:header_end] + section + text[header_end:]
    else:
        text = '# Changelog\n' + section

    CHANGELOG.write_text(text)


def prepare(args):
    """Prepare a release: bump version files and generate changelog."""
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

    # Safety: all tests must pass
    print('Running tests...')
    result = subprocess.run(
        [sys.executable, '-m', 'pytest', 'tests/', '-q'],
        cwd=ROOT)
    if result.returncode != 0:
        print('Error: tests failed. Fix them before releasing.',
              file=sys.stderr)
        sys.exit(1)

    # Generate release notes
    previous_tag = get_previous_tag()
    notes = get_release_notes(previous_tag)

    # Update files
    replace_in_file(
        PYPROJECT, f'version = "{old_str}"', f'version = "{new_str}"')
    replace_in_file(
        INIT_PY, f'__version__ = "{old_str}"', f'__version__ = "{new_str}"')
    update_changelog(new_str, notes)

    print(f'{old_str} -> {new_str}')
    print()
    if notes:
        print('Release notes:')
        for note in notes:
            print(f'  - {note}')
        print()
    print('Review CHANGELOG.md, then run:')
    print('  python scripts/bump-version.py --finalize')


def finalize(args):
    """Commit and tag the prepared release."""
    version = read_version(PYPROJECT, r'version\s*=\s*"([^"]+)"')
    tag = f'v{version}'

    # Safety: tag must not exist (confirms prepare ran but finalize hasn't)
    existing_tags = run(['git', 'tag', '--list', tag])
    if existing_tags.stdout.strip():
        print(f'Error: tag {tag} already exists.', file=sys.stderr)
        sys.exit(1)

    # Commit and tag
    commit_msg = f'Release git-p4son v{version}'
    run(['git', 'add', str(PYPROJECT), str(INIT_PY), str(CHANGELOG)])
    run(['git', 'commit', '-m', commit_msg])
    run(['git', 'tag', tag])

    print(f'Committed: {commit_msg}')
    print(f'Tagged: {tag}')
    print()
    print('To publish, push the commit and tag:')
    print(f'  git push && git push origin {tag}')


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
