"""
Prototype: cache of which tracked files may carry local divergence.

A file needs the post-sync merge only if its content differs from the last
Perforce state git recorded for it. That is decided today by walking history
per file (find_base_commits), which is the dominant cost of a sync when many
files are candidates, e.g. on an allwrite workspace where every synced file
is writable.

The cheap invariant this module exploits: if the newest commit touching a
file is a sync commit, then that commit is the file's baseline and the file
cannot have diverged. Such files are "clean" and can skip classification.
Anything else, including anything the cache has never seen, stays a
candidate, so being wrong can only cost time, never a missed merge.

The clean set is maintained incrementally. Each sync walks only the commits
added since the cache was written, which is a handful, and re-marks every
path those commits touched. The cache starts empty, so the first sync after
enabling it behaves exactly as before and each sync teaches it the paths it
touched.

Stored in .git-p4son/divergence.cache, alongside the other local state and
kept out of version control. Enabled with GIT_P4SON_DIVERGENCE_CACHE=1.
"""

import os
from dataclasses import dataclass, field

from . import CONFIG_DIR
from .git import is_ancestor, newest_touch_is_sync
from .state import ensure_gitignored

CACHE_FILE = 'divergence.cache'
ENABLE_ENV = 'GIT_P4SON_DIVERGENCE_CACHE'


@dataclass
class DivergenceCache:
    """Paths whose newest touching commit is a sync commit, as of commit."""
    commit: str
    clean: set[str] = field(default_factory=set)


def is_enabled() -> bool:
    """Whether the divergence cache is turned on for this run."""
    return os.environ.get(ENABLE_ENV, '') not in ('', '0', 'false')


def cache_path(workspace_dir: str) -> str:
    """Return the path to the divergence cache file."""
    return os.path.join(workspace_dir, CONFIG_DIR, CACHE_FILE)


def load(workspace_dir: str) -> DivergenceCache | None:
    """Read the cache, or None when it is missing or unreadable."""
    path = cache_path(workspace_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except OSError:
        return None
    if not lines or not lines[0].strip():
        return None
    return DivergenceCache(commit=lines[0].strip(),
                           clean={line for line in lines[1:] if line})


def save(workspace_dir: str, cache: DivergenceCache) -> None:
    """Write the cache, keeping it out of version control."""
    path = cache_path(workspace_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(cache.commit + '\n')
        for rel in sorted(cache.clean):
            f.write(rel + '\n')
    ensure_gitignored(workspace_dir, CACHE_FILE)


def advance(cache: DivergenceCache, head_commit: str,
            workspace_dir: str) -> DivergenceCache | None:
    """Move the cache forward to head_commit.

    Returns None when the cached commit is not an ancestor of head_commit
    (branch switch, rebase, amend); the recorded verdicts may then rest on
    commits that are no longer reachable, so the cache has to be rebuilt."""
    if cache.commit == head_commit:
        return cache
    if not is_ancestor(cache.commit, head_commit, workspace_dir):
        return None
    kinds = newest_touch_is_sync(
        [f'{cache.commit}..{head_commit}'], workspace_dir)
    clean = set(cache.clean)
    for path, touched_by_sync in kinds.items():
        if touched_by_sync:
            clean.add(path)
        else:
            clean.discard(path)
    return DivergenceCache(commit=head_commit, clean=clean)


def clean_paths(head_commit: str, workspace_dir: str) -> set[str]:
    """Paths known not to need classification at head_commit.

    Empty whenever the cache is off, missing or stale, which puts every file
    back through the full check."""
    if not is_enabled():
        return set()
    cache = load(workspace_dir)
    if cache is None:
        return set()
    advanced = advance(cache, head_commit, workspace_dir)
    if advanced is None:
        return set()
    if advanced is not cache:
        save(workspace_dir, advanced)
    return advanced.clean


def record_sync(pre_sync_head_commit: str, post_sync_head_commit: str,
                workspace_dir: str) -> None:
    """Fold the commits this sync created into the cache.

    Seeds an empty cache at the pre-sync commit first, so the very first run
    already learns every path the sync just committed."""
    if not is_enabled():
        return
    cache = load(workspace_dir)
    if cache is None:
        cache = DivergenceCache(commit=pre_sync_head_commit)
    advanced = advance(cache, post_sync_head_commit, workspace_dir)
    if advanced is None:
        advanced = DivergenceCache(commit=post_sync_head_commit)
    save(workspace_dir, advanced)
