#!/usr/bin/env python3
"""Bump the version of git-p4son.

Workflow, run from an up-to-date main:
  1. bump-version.py [patch|minor|major]: bump the version files and move the
     entries under "## Unreleased" in CHANGELOG.md under the new version
  2. (edit CHANGELOG.md if desired)
  3. bump-version.py --finalize: commit on a release-vX.Y.Z branch, push it
     and open a pull request
  4. (merge the pull request once CI passes)
  5. bump-version.py --publish: tag the merged release commit on main, push
     the tag, which publishes to PyPI, and create the GitHub release
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


def changelog_section(version, text=None):
    """Return the CHANGELOG.md section for a version, without its heading, from text or else the file."""
    text = CHANGELOG.read_text() if text is None else text
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

    # Safety: release from an up-to-date main, before changing any file
    require_up_to_date_main()

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


def run_steps(steps):
    """Run (message when done, command, stdin, command to show) steps; on a failure, list the rest to run by hand."""
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


def require_gh():
    """Exit unless the GitHub CLI is installed and logged in."""
    if not shutil.which('gh'):
        print('Error: the GitHub CLI (gh) is needed for the release.',
              file=sys.stderr)
        sys.exit(1)
    run(['gh', 'auth', 'status'])


def require_no_tag(tag, hint=''):
    """Exit, with hint after the error, if tag exists locally or on origin."""
    local = run(['git', 'tag', '--list', tag]).stdout.strip()
    remote = run(['git', 'ls-remote', '--tags', 'origin',
                  f'refs/tags/{tag}']).stdout.strip()
    if local or remote:
        print(f'Error: tag {tag} already exists.{hint}', file=sys.stderr)
        sys.exit(1)


def require_up_to_date_main():
    """Exit unless main is checked out at origin/main."""
    run(['git', 'fetch', '--quiet', 'origin'])
    current = run(['git', 'rev-parse', '--abbrev-ref', 'HEAD']).stdout.strip()
    if current != 'main':
        print('Error: run this on main.', file=sys.stderr)
        sys.exit(1)
    head = run(['git', 'rev-parse', 'HEAD']).stdout.strip()
    if head != run(['git', 'rev-parse', 'origin/main']).stdout.strip():
        print('Error: main is not at origin/main. Pull first.',
              file=sys.stderr)
        sys.exit(1)


def release_names(version):
    """The tag, release branch and release commit subject for a version."""
    return (f'v{version}', f'release-v{version}',
            f'Release git-p4son v{version}')


def finalize(args):
    """Commit the prepared release on a release branch, push it and open a pull request."""
    version = read_version(PYPROJECT, r'version\s*=\s*"([^"]+)"')
    tag, branch, subject = release_names(version)

    # Check everything before committing, so a failure leaves no half-done
    # release behind
    require_no_tag(tag)
    require_gh()
    require_up_to_date_main()
    if (run(['git', 'branch', '--list', branch]).stdout.strip()
            or run(['git', 'ls-remote', '--heads', 'origin',
                    f'refs/heads/{branch}']).stdout.strip()):
        print(f'Error: branch {branch} already exists.', file=sys.stderr)
        sys.exit(1)
    notes = changelog_section(version)

    # Commit on the release branch; --no-track keeps a plain push off main
    run(['git', 'switch', '--quiet', '--create', branch, '--no-track'])
    run(['git', 'add', str(PYPROJECT), str(INIT_PY), str(CHANGELOG)])
    run(['git', 'commit', '-m', subject])
    print(f'Committed on {branch}: {subject}')

    pr_cmd = ['gh', 'pr', 'create', '--base', 'main', '--head', branch,
              '--title', subject, '--body-file']
    run_steps([
        (f'Pushed {branch}',
         ['git', 'push', '--set-upstream', 'origin', f'{branch}:{branch}'],
         None, None),
        (f'Opened the pull request for {branch}', pr_cmd + ['-'], notes,
         f'{shlex.join(pr_cmd)} <the {version} section of CHANGELOG.md>'),
    ])
    print()
    print('Merge the pull request once CI passes, then run:')
    print('  python scripts/bump-version.py --publish')


def publish(args):
    """Tag the merged release commit on main, push the tag and create the GitHub release."""
    # --finalize leaves the release branch checked out, which has the version.
    version = read_version(PYPROJECT, r'version\s*=\s*"([^"]+)"')
    tag, branch, _ = release_names(version)

    require_no_tag(tag, ' Check out the release branch, or pull main once '
                   'the release pull request is merged. If a failed push left '
                   f'the tag behind locally, delete it: git tag -d {tag}')
    require_gh()
    # GitHub's merge commit is the release commit as it is on main, whether
    # the pull request was rebase-merged or squashed.
    pr = run(['gh', 'pr', 'view', branch, '--json', 'state,mergeCommit',
              '--jq', '.state + " " + (.mergeCommit.oid // "")'])
    state, _, commit = pr.stdout.strip().partition(' ')
    if state != 'MERGED' or not commit:
        print(f'Error: the pull request for {branch} is {state.lower()}, '
              'not merged.', file=sys.stderr)
        sys.exit(1)
    run(['git', 'fetch', '--quiet', 'origin'])
    if subprocess.run(['git', 'merge-base', '--is-ancestor', commit,
                       'origin/main']).returncode:
        print(f'Error: merge commit {commit[:7]} of {branch} is not on '
              'origin/main.', file=sys.stderr)
        sys.exit(1)
    notes = changelog_section(
        version, run(['git', 'show', f'{commit}:CHANGELOG.md']).stdout)

    run(['git', 'tag', tag, commit])
    print(f'Tagged {commit[:7]}: {tag}')

    # Pushing the tag starts the PyPI publish workflow.
    release_cmd = ['gh', 'release', 'create', tag, '--verify-tag', '--latest',
                   '--title', f'git-p4son {tag}', '--notes-file']
    run_steps([
        (f'Pushed {tag}', ['git', 'push', 'origin', f'refs/tags/{tag}'],
         None, None),
        (f'Created GitHub release {tag}', release_cmd + ['-'], notes,
         f'{shlex.join(release_cmd)} <the {version} section of '
         'CHANGELOG.md>'),
    ])

    # The merged commit has the release branch's content; drop the branch.
    if (run(['git', 'branch', '--list', branch]).stdout.strip()
            and not subprocess.run(['git', 'diff', '--quiet', branch, commit])
            .returncode):
        if run(['git', 'rev-parse', '--abbrev-ref',
                'HEAD']).stdout.strip() == branch:
            run(['git', 'switch', '--quiet', 'main'])
        run(['git', 'branch', '--quiet', '-D', branch])
        print(f'Deleted the merged branch {branch}')


def main():
    parser = argparse.ArgumentParser(description='Bump the git-p4son version.')
    parser.add_argument(
        'part',
        nargs='?',
        default='patch',
        choices=['major', 'minor', 'patch'],
        help='Which part to bump (default: patch)'
    )
    step = parser.add_mutually_exclusive_group()
    step.add_argument(
        '--finalize',
        action='store_true',
        help='Commit the prepared release on a release branch and open a pull '
             'request'
    )
    step.add_argument(
        '--publish',
        action='store_true',
        help='Tag the merged release commit, push the tag and create the '
             'GitHub release'
    )
    args = parser.parse_args()

    if args.finalize:
        finalize(args)
    elif args.publish:
        publish(args)
    else:
        prepare(args)


if __name__ == '__main__':
    main()
