# Brief

Reviewing `_merge_changed_files` in `git_p4son/sync.py` I realize that we the
merge local changes algorithm is probably not doing what it is supposed to.

First thing, we need to find the true merge base. Say that file A.cpp is detected writable
in `prepare_writable_files` and is queued up for merge.
The `merge-base` can be found via the last `git-p4son: p4 sync ...` commit that touched it the last time
before the CURRENT sync.
`ours` can be found via the previous commit to CURRENT sync commit (something like `HEAD~1`).
`theirs` is then found in the CURRENT sync commit (`HEAD`).

Does that seem reasonable? Write your analysis below. Don't implement anything until I say so.

# Analysis

## TL;DR

Your reasoning is correct and identifies a real bug. The current `_merge_changed_files`
uses the *most recent* sync commit as the base, but the correct base is the most recent
sync commit **that actually modified the file**. When the previous sync didn't touch
the file in question, the current algorithm silently discards the user's local
modifications.

## How the current code maps to (base, ours, theirs)

In `sync.py:480-482`, the merge is called after the new sync commit has been created.
The mapping is:

- `base` <- file content at `last_sync_commit`, which is `git_last_sync(...)` (the most
  recent commit whose subject matches `: p4 sync //...`). See `sync.py:438`.
- `ours` <- file content at `head_commit`, which is `git rev-parse HEAD` captured
  *before* the sync ran. After the sync commit is created, this is equivalent to
  `HEAD~1`. See `sync.py:435`.
- `theirs` <- file read from disk after `p4 sync`. Since `add_all_files` was just run,
  this is equivalent to reading from `HEAD`.

So your proposal for `ours` (HEAD~1) and `theirs` (HEAD) is exactly equivalent to what
the code does today, just expressed using git refs after the commit has landed instead
of capturing pre-sync state. That part is a cosmetic simplification, not a bug fix.

The interesting part is `base`.

## The bug: `base` is wrong when the previous sync didn't touch the file

The current code picks `base = last_sync_commit` regardless of whether that commit
actually changed the file. That breaks in this scenario:

1. Sync `S0` (CL 100): `A.cpp` = X.
2. User commit `C1`: user makes a local writable edit, `A.cpp` = Y, and commits it on
   a feature branch.
3. Sync `S1` (CL 150): the changelist does not include `A.cpp`. `p4_sync_preview`
   does not return it, `prepare_writable_files` never sees it, `p4 sync` does not
   touch it. `add_all_files` then commits the working tree as-is, so the tree object
   in `S1` records `A.cpp` = Y (inherited unchanged from `C1`).
4. Sync `S2` (CL 200, the current sync): the changelist *does* include `A.cpp` and
   p4 clobbers it to W on disk. `prepare_writable_files` queues `A.cpp` for merge.
5. `_merge_changed_files` runs:
   - `ours` = file at `HEAD~1` = Y (correct).
   - `base` = file at `last_sync_commit` = file at `S1` = **Y** (wrong - it should be X).
   - `theirs` = W.
   - Three-way merge sees `base == ours`, so the result is `theirs` = W.
   - The user's X->Y diff is silently dropped on disk.

The user's commit `C1` still exists in git history, so this is not git-level data
loss, but the merge tool's job is to produce a working-tree starting point for the
user to review, and it fails to do so.

## Why your "last sync commit that touched it" framing fixes this

The merge base for a per-file three-way merge should be the last upstream content the
user diverged from. "Upstream" here is the chain of `git-p4son: p4 sync` commits, and
a sync commit only contributes new upstream content for files it actually modifies.
Intermediate sync commits that did not modify the file are pass-throughs and should
not be treated as the divergence point.

For each file we want the merge for, the right query is roughly:

```
git log --grep=': p4 sync //' --pretty=%H -1 <pre-sync-HEAD> -- <file>
```

i.e. the most recent commit, reachable from the commit before the current sync, whose
subject matches the sync pattern and which actually modified `<file>`. `git log -- <path>`
already filters to commits that touched the path, so `-1` gives us the right one.
Use the existing subject regex from `git_last_sync` so the grep matches the same
`(\d+|pergit|git-p4son): p4 sync //` family of subjects.

If no such commit exists (the file is new to p4 from the user's perspective, e.g.
created by this sync), fall back to `base = b''` like today.

## Considerations and edge cases

- **Re-checking `ours`.** Since the bug above can leave `base` correct but the file
  on disk wrong, switching to `HEAD~1` for `ours` is fine but not load-bearing. The
  fix is entirely about `base`. We can still keep capturing `head_commit` pre-sync
  for clarity, or switch to `HEAD~1` after the commit - both reach the same content.

- **Per-file base lookup cost.** This adds one `git log` invocation per writable
  changed file. In practice the changed-file count is small (writable, non-ignored,
  MD5-divergent). If it ever becomes a concern, a single `git log --name-only --grep`
  scan walking back from `HEAD~1` can populate a map of file -> last-touching-sync
  in one pass.

- **`--force` sync to an older CL.** The same logic still works: we look backward
  from the pre-current-sync HEAD for the last sync that touched the file. The
  direction of the upstream move doesn't matter for picking the base.

- **File deleted in an earlier sync and re-added later.** "Last sync that touched
  the file" naturally picks the re-add (or the delete) - whichever came last. The
  content at that commit is the right base. If `base` is `None` (deletion) we fall
  back to `b''` as today.

- **Two-pass sync.** The current code does pass-1 (to `last_changelist`) and pass-2
  (to target `changelist`) before committing. The set of changed files is the union
  across both passes. The per-file base lookup is independent of which pass surfaced
  the file, so this works unchanged.

- **First-ever sync.** If there's no prior sync commit at all, `git log --grep=...`
  returns nothing for every file and we fall back to `base = b''`. Same as today.

## Summary

Yes, your framing is reasonable, and it fixes a real bug. The concrete change is:
swap `last_sync_commit` (single commit for all files) for a per-file "last sync
commit that touched this file" lookup, computed against the pre-current-sync HEAD.
`ours` and `theirs` can stay as they are, or be re-expressed as `HEAD~1` and `HEAD`
respectively after the sync commit is created - that's a style choice, not a fix.

# Implementation plan

The change is small and localized: one new helper in `git.py`, a tweaked merge
loop in `sync.py`, and a removed parameter. `ours` and `theirs` stay as they are
today (keep `head_commit` captured pre-sync, keep reading `theirs` from disk).
Only `base` changes from a single commit to a per-file lookup.

## 1. Add `find_last_sync_commit_for_file` in `git_p4son/git.py`

Place it next to `get_file_at_commit` in the "file retrieval" section.

```python
def find_last_sync_commit_for_file(filepath: str, before_commit: str,
                                   workspace_dir: str) -> str | None:
    """Return SHA of the most recent sync commit reachable from before_commit
    (inclusive) that modified filepath, or None if none exists.
    filepath is repo-relative."""
    git_path = filepath.replace('\\', '/')
    result = run(
        ['git', 'log', '-1', '--pretty=%H',
         '--grep=: p4 sync //',
         before_commit, '--', git_path],
        cwd=workspace_dir, fail_on_returncode=False)
    if result.returncode != 0 or not result.stdout:
        return None
    sha = result.stdout[0].strip()
    return sha or None
```

The `--grep=: p4 sync //` argument matches the subject family used by `git_last_sync`
(`(\d+|pergit|git-p4son): p4 sync //...`). We intentionally don't post-filter with
the full regex - the substring is specific enough, and even an unlikely false match
would just yield a reasonable file snapshot to use as base.

## 2. Update `_merge_changed_files` in `git_p4son/sync.py`

Drop the `last_sync_commit` parameter and rename `head_commit` to
`pre_sync_head_commit` (the existing name is misleading - by the time the merge
runs, HEAD is the new sync commit, not this one). The rename applies to the
parameter here and to the local in `sync_command` (currently `sync.py:435`).
For each file, look up its own base commit from `pre_sync_head_commit`.

Signature becomes:

```python
def _merge_changed_files(changed_files: list[str], pre_sync_head_commit: str,
                         workspace_dir: str,
                         binary_files: set[str] | None = None) -> None:
```

Inside the per-file loop, replace the current `base` lookup:

```python
# Before:
base = None
if last_sync_commit:
    base = get_file_at_commit(rel_path, last_sync_commit, workspace_dir)

# After:
base = None
base_commit = find_last_sync_commit_for_file(
    rel_path, pre_sync_head_commit, workspace_dir)
log.info(f'{rel_path}: base = {base_commit or "(none)"}')
if base_commit:
    base = get_file_at_commit(rel_path, base_commit, workspace_dir)
```

The rest of the function (add/delete asymmetry, binary handling, three-way merge,
reporting) is unchanged. The `if base is None: base = b''` fallback at line 231
still covers "no prior sync touched this file."

Also drop the now-stale `log.info(f'last_sync_commit: {last_sync_commit}')` debug
line at `sync.py:171`; the per-file log above replaces it and is more useful for
live debugging.

## 3. Update the call site in `sync_command`

At `sync.py:480-482`, drop the `last_sync_commit` argument and pass the renamed
local:

```python
_merge_changed_files(changed_files, pre_sync_head_commit,
                     workspace_dir, binary_files=all_binary)
```

Rename the local at `sync.py:435` from `head_commit` to `pre_sync_head_commit`
(and update the surrounding `log.success(...)` line). Remove the now-unused
`last_sync_commit = last_sync.commit if last_sync else None` line at
`sync.py:438`. `last_sync.changelist` is still used elsewhere (`last_changelist`,
the older-CL guard, the two-pass sync), so `last_sync` itself stays.

## 4. Tests

Add a test for `find_last_sync_commit_for_file` in `tests/` that builds a small
temp repo with a mix of sync-subject and user-subject commits, plus commits that
do and don't touch the target file, and asserts:

- Returns the most recent sync commit that touched the file.
- Skips sync commits that didn't touch the file.
- Skips user commits even when they did touch the file.
- Returns `None` when no sync commit touched the file.
- Respects the `before_commit` bound (a sync commit reachable only from a
  descendant of `before_commit` is not returned).

If feasible, also add an end-to-end test for the bug scenario in the analysis:
S0 modifies A, C1 user-edits A, S1 doesn't touch A but inherits the user's
content into its tree, S2 modifies A; assert that the merged on-disk result
preserves the user's edits instead of taking `theirs` wholesale. This may
require enough p4 mocking that a focused unit test on `_merge_changed_files`
with a hand-built git history is more practical.

## 5. Formatting and verification

- Run `autopep8 -i -r git_p4son/ tests/`.
- Run `python -m pytest tests/`.

## Out of scope

- Switching `ours`/`theirs` to `HEAD~1`/`HEAD` refs. Equivalent to current behavior;
  not part of the fix.
- Batching the per-file `git log` into a single walk. Premature; revisit only if
  sync time on large changelists becomes a problem.
- Changing the merge UX (labels, conflict markers, reporting). Untouched.
