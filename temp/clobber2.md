# Brief

In @clobber.md we outlined a plan how to get around the writable file/clobber issue you get when you combine
perforce with git.

Currently we try to sync, discover what files perforce refuse to write because of clobber issues, and then
resolve. It seems to work quite well, but I think we can improve so that it does not feel as brute force.

What if we first see what files that will be synced using `p4 sync -n`, and then we walk through those files
to ensure that the read-only flag is still set. If it is not we set it, before we issue the real `p4 sync` command.
We only do this for the files that git DOES NOT ignore, and we need to store the files for later so that
we resolve them like we do today (with md5 compares and all that. 

Write your analysis of this idea below. Don't implement anything until I say so.

## Analysis

### What it buys you

- **Cleaner flow.** Instead of relying on stderr parsing of "Can't clobber writable file" errors, you know upfront
  which files will be touched and can prepare for them. No error-recovery loop.
- **Fewer force-syncs.** Currently, every clobbered file needs an individual `p4 sync -f` call. If you set the
  read-only flag before the real sync, Perforce writes the file normally - no force-sync needed.
- **Single sync pass potential.** The current two-pass sync (first to last-synced CL, then to target CL) exists partly
  to handle clobber errors incrementally. A pre-check approach might simplify this.

### Considerations

1. **`p4 sync -n` cost.** The preview hits the Perforce server but transfers no file content - just the list of files
   that would be synced. This is lightweight compared to the actual sync and is an acceptable cost.

2. **Race conditions are not our problem.** Between `p4 sync -n` and `p4 sync`, a file could theoretically become
   writable again - but that requires the user actively modifying files during a sync, which they shouldn't do. If
   clobber errors still occur during the real sync, we simply bail out. No fallback recovery needed for git-tracked
   files. For git-ignored files that are writable on disk, we already know about them from the preview step. We store
   these as "expected clobber" files so we don't abort the sync - instead we print them at the end to notify the user
   that p4son could not sync them.

3. **No need to store file contents.** The local file content is already in git's index. After the sync commit is made,
   we can use `get_file_at_commit` with `HEAD~1` to retrieve the pre-sync version for three-way merging. We only need
   to store the filenames of writable files, not their contents.

4. **Read-only flag check is cheap.** We check the read-only flag on all files from `p4 sync -n` (a local stat call).
   Only writable files need the `git check-ignore` call. This filters down to a small set efficiently.

### Revised flow

1. Run `p4 sync -n` to get the list of files that will be synced.
2. For each file in the list, check if it is writable on disk.
3. For writable files, run `git check-ignore` to separate ignored from tracked.
   - **Ignored + writable:** Store as "expected clobber" - these will fail during real sync and that's OK.
   - **Tracked + writable:** Set the read-only flag so Perforce can overwrite them. Store filenames for post-sync
     resolution.
4. Run the real `p4 sync`. If clobber errors occur for files not in the "expected clobber" set, bail out.
5. Create the sync commit.
6. For the stored writable-tracked filenames, use `get_file_at_commit(HEAD~1)` to get pre-sync content, then do
   MD5 comparison and three-way merge as before.
7. Print any "expected clobber" (ignored) files at the end so the user knows they weren't synced.

### Verdict

This approach is cleaner than the current error-recovery loop. The `p4 sync -n` cost is minimal, we don't need to
store file contents (git has them), and we can drop the force-sync and clobber-recovery machinery entirely. The only
files that will still produce clobber errors are git-ignored writable files, which we handle gracefully by expecting
and reporting them.

## Implementation plan

### Step 1: Add `p4_sync_preview()` to `perforce.py`

New function that runs `p4 sync -n <depot_root>/...@<changelist>` and returns the list of local file paths that would
be synced. Reuse `parse_p4_sync_line()` to extract filenames from the output. The `p4 sync -n` output format is
identical to regular sync output (e.g. `//depot/foo#2 - updating /ws/foo`).

Return a list of filenames (the local paths), ignoring the mode (add/del/upd) since we only need to know which files
will be touched.

```python
def p4_sync_preview(changelist: int, depot_root: str,
                    workspace_dir: str) -> list[str]:
```

### Step 2: Add `_prepare_writable_files()` to `sync.py`

New function that takes the preview file list, checks read-only flags, classifies writable files, and makes them
read-only so `p4 sync` can overwrite them.

```python
@dataclass
class WritableSyncFileSet:
    """Result of preparing writable files before sync."""
    tracked: list[str]     # writable + git-tracked, made read-only, need post-sync merge
    ignored: list[str]     # writable + git-ignored, will clobber during sync

def prepare_writable_files(preview_files: list[str],
                           workspace_dir: str) -> WritableSyncFileSet:
```

Logic:
1. For each file in `preview_files`, check if it exists on disk and is writable (`os.stat` + `stat.S_IWUSR`).
2. Collect all writable files. If none, return empty `WritableSyncFileSet`.
3. Run `get_ignored_files()` on the writable files to split into ignored vs tracked.
4. For tracked writable files: remove the write permission (`os.chmod`).
5. Return `WritableSyncFileSet` with both lists.

### Step 3: Rework `p4_sync()` in `sync.py`

Change `p4_sync()` to accept an optional set of expected clobber files (the ignored writable files from step 2). When
a `RunError` occurs with clobber errors:
- If all clobbered files are in the expected set, log them and continue (don't raise).
- If any clobbered file is NOT in the expected set, raise the error (bail out).

Update the return type: instead of returning a list of writable files, return nothing (the clobber recovery loop is
gone). On unexpected clobber, raise.

```python
def p4_sync(changelist: int, label: str, depot_root: str,
            workspace_dir: str,
            expected_clobber: set[str] | None = None) -> None:
```

### Step 4: Rework `sync_command()` in `sync.py`

Replace the clobber recovery with preview + prepare, keeping the two-pass sync structure:

The two-pass sync must stay. The first sync to the last-synced CL resets Perforce's sync state so that files the user
submitted in between (which got CL numbers higher than the last p4son sync) get re-downloaded in the second sync. This
is essential for building a correct git commit.

Since both syncs can encounter writable files, we run preview + prepare before each:

```
 1. [existing] Validate git/p4 workspaces, find changelists, etc.
 2. [existing] Remember user_commit and last_sync_commit

    --- First pass: sync to last-synced CL ---
 3. [new]      p4_sync_preview() to last-synced CL
 4. [new]      prepare_writable_files() on preview results -> prep1
 5. [changed]  p4_sync() to last-synced CL, passing expected_clobber=prep1.ignored

    --- Second pass: sync to target CL ---
 6. [new]      p4_sync_preview() to target CL
 7. [new]      prepare_writable_files() on preview results -> prep2
 8. [changed]  p4_sync() to target CL, passing expected_clobber=prep2.ignored

 9. [existing] Commit (pure Perforce state)
10. [existing] _merge_changed_files() with combined prep1.tracked + prep2.tracked
11. [new]      Print ignored clobber files at the end if any (combined from both passes)
```

The preview + prepare calls are lightweight (no file content transfer, just stat calls), so doubling them for the
two-pass structure is fine.

Use `log.heading()`, `log.success()`, `log.warning()`, and `log.info()` throughout the new flow steps so the user
can follow what's happening - consistent with how the existing sync steps are logged.

### Step 5: Update `_merge_changed_files()` in `sync.py`

No changes needed. The function already works correctly for this flow: it receives absolute file paths (which is what
`parse_p4_sync_line` produces), retrieves `ours` from `get_file_at_commit(rel_path, user_commit, ...)`, and reads
`theirs` from disk (post-sync).

### Step 6: Remove old clobber machinery

Delete or simplify the following once the new flow is working:

- **`resolve_writable_files()`** in `sync.py` - no longer needed (replaced by `prepare_writable_files`)
- **`WritableResolution`** dataclass in `sync.py` - replaced by `WritableSyncFileSet`
- **`_log_resolution()`** in `sync.py` - replaced by inline logging in the new flow
- **`p4_force_sync_file()`** in `perforce.py` - no longer needed (no force-syncing)
- **`get_writable_files()`** in `perforce.py` - still needed in reworked `p4_sync()` for expected clobber check
- **`compute_local_md5()`** in `common.py` - no longer needed (git index replaces MD5 comparison)
- **`p4_fstat_digests()`** in `perforce.py` - no longer needed

Also update imports in `sync.py` to remove unused references.

### Step 7: `P4SyncOutputProcessor` - no changes

Keep `P4SyncOutputProcessor` as-is including the `clb` tracking. Git-ignored writable files will still produce
clobber errors during the real sync, so the processor needs to handle them.

### Step 8: Update tests

- **New tests for `p4_sync_preview()`**: mock `run_with_output`, verify parsing of preview output.
- **New tests for `prepare_writable_files()`**: test with writable/read-only/ignored combinations, verify chmod calls.
- **Update `TestP4Sync`**: test expected clobber behavior (expected files tolerated, unexpected files raise).
- **Update `TestSyncCommand`**: adjust mocks for new flow (preview + single sync instead of two-pass + resolve).
- **Remove `TestResolveWritableFiles`**: replaced by `prepare_writable_files` tests.
- **Keep `TestMergeChangedFiles`**: unchanged, still validates the merge logic.
