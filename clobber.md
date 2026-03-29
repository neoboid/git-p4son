# Brief
In "Why clobber?" in @README.md we go through why p4son requires clobber to
be enabled on the users workspace.

Maybe we can solve that problem in another way, without the clobber flag.

What if we during `p4 sync` command:
* collect all files that fail to sync because p4 say `Can't clobber writable file`
* Go through each of these files:
  * For files that is managed by git (not ignored by `.gitignore`)
    * Comparing the file on disk against file in p4 has at `last-synced` CL
        * If identical its just the read only flag that has been removed by git, and we can force sync the file
        * If changed we skip syncing it for now, but store it on a list for later processing
  * For files ignored by git we don't want to overwrite the file, let's issue a warning that the file could not be
    synced and the user needs to manually deal with it, as it not p4sons concern.
* The changed files on disk is now commited as usual, with the `git p4son: p4 sync //<workspace/...@1234` message.

Now we have a list of files that is changed on disk as p4 is concerned. But as `git status` has been verified to
be clean it must mean that a commit has happened on `main` where the file was manually changed but not committed to
perforce. To me it would be most useful to actually see what changed upstream in these files and resolve that
manually, the easiest way would be to simply force sync the files and leave them as unstaged in git.
The user can then resolve and commit this change manually.
The sync process ends by listing all these files along with a note what the user needs to do.

This likely means that we nolonger need the `--force` flag to `sync` command-.

## Analysis

### The core insight is sound

The idea correctly identifies that the "Can't clobber writable file" errors during `p4 sync` fall into distinct
categories that can be handled differently. This is a good approach because it removes a setup requirement that confuses
users and makes p4son feel invasive.

### How it maps to the current code

The existing code already parses clobber errors - `get_writable_files()` in `sync.py` extracts the file list from
stderr. Today, the only options are: bail out, or `--force` to blindly overwrite everything. The proposal replaces this
binary choice with smarter per-file logic.

### Writable file categories

Each writable file encountered during sync falls into one of four categories. These names are used throughout this
document and should be used in the code as well.

**writable-unchanged**: Git-tracked file, identical to last-synced version.
This is the common case. Git removes read-only flags on checkout, so every file that differs between branches becomes
writable. When you switch back to `main` for a sync, the file content matches what p4 has, but the flag is gone. Safe
to `p4 sync -f` automatically.

Use `p4 fstat -T digest` to get the MD5 checksum from Perforce, then compute the local MD5 and compare. This avoids
downloading file contents and is fast even for large files.

**writable-changed**: Git-tracked file, content differs from last-synced version.
This means someone committed a change to `main` in git without submitting it to Perforce. Rather than simply showing
what the user lost, we can rebase the user's local changes on top of the new Perforce state:

1. Retrieve the user's version from git (it's already stored in `.git` from the previous commit on `main`).
2. Force sync the file from Perforce to get the latest version.
3. Include the force-synced file in the normal sync commit (so the commit reflects pure Perforce state).
4. Use `git merge-file` to three-way merge the user's version against the last-synced base and the new Perforce
   version, then write the merged result back to disk.
5. If the merge is clean, the user sees their changes reapplied on top of the latest Perforce content in `git diff`.
   If there are conflicts, the file contains standard conflict markers for the user to resolve manually.

This gives the user the same experience as a git rebase - their local changes are preserved and composed with upstream
changes, with conflicts surfaced clearly.

**added-local-deleted-upstream**: File added locally but deleted in Perforce.
After force sync the file is gone. Restore the user's version to disk as an untracked file and warn that Perforce
deleted it - the user decides whether to keep or discard it.

**deleted-local-added-upstream**: File deleted locally but modified in Perforce.
The file doesn't exist in the user's commit on `main`. After force sync brings in the new Perforce version, leave it
as-is in the sync commit. No merge needed - the user sees the new content and can delete it again if they want.

Both add/delete cases should be logged as warnings so the user is aware of the asymmetry and can act on it.

**ignored**: Git-ignored file.
Warn and skip. p4son shouldn't touch files outside git's domain.

### Considerations

1. **Two-pass sync**: The initial `p4 sync` will partially succeed (syncing non-writable files) and partially fail
   (writable files). You then force-sync some of those files individually. This is fine - p4 handles partial syncs
   well, and `p4 sync -f` on individual files is idempotent.

2. **Performance**: Batch all writable files into a single `p4 fstat -T digest` call to get all upstream MD5s at once,
   then compute local MD5s to compare. This keeps writable-unchanged resolution fast even with many writable files.

3. **The `--force` flag**: It becomes mostly unnecessary for clobber handling. However, it's still needed for syncing
   to an older changelist (sync.py line 211-216). Keep it for that purpose but remove the clobber-related semantics.

4. **The `init` clobber check**: Remove the clobber requirement from `init.py` and the setup instructions in the
   README. This is a nice UX win.

5. **Edge case - file added by p4 that already exists on disk**: If p4 wants to add a new file but the path already
   exists as a writable file, this is also a clobber error. The logic should handle this - if git tracks the file,
   same classification applies (writable-unchanged or writable-changed). If ignored, warn and skip.

6. **Commit ordering**: Force-synced writable-unchanged and writable-changed files are both included in the sync
   commit, so it represents pure Perforce state. After the commit, writable-changed files have their merged content
   written back to disk,
   appearing as unstaged changes in `git diff`.

### Summary

The idea is solid and well thought out. Using batched `p4 fstat -T digest` for checksum comparison keeps it fast.
The three-way merge for writable-changed files gives users the same rebase-like experience they're used to from git -
changes are preserved and composed with upstream, with conflicts surfaced via standard markers. The end result: no
clobber flag required, no `--force` needed for normal syncs, and a smooth experience when local changes exist.

---

## Implementation plan

### Step 1: Add new helper functions to `perforce.py`

**`p4_fstat_digests(filenames, changelist, workspace_dir) -> dict[str, str]`**
Runs `p4 fstat -T digest <file1>@CL <file2>@CL ...` in a single call. Parses the ztag-style multi-record output and
returns a mapping of local path to MD5 digest. Uses `parse_ztag_multi_output()` which already exists.

Since `p4 fstat` uses depot paths but the clobber errors report local paths, use `-T digest,clientFile` to get both
fields, then key the dict by local path for easy lookup.

### Step 2: Add new helper functions to `git.py`

**`get_ignored_files(filepaths, workspace_dir) -> set[str]`**
Runs `git check-ignore` with all paths passed as arguments. Returns the set of paths that are ignored. `git check-ignore`
exits 0 if any path matches, 1 if none match - we parse stdout for the matched paths regardless of exit code. No
batching needed - the OS `ARG_MAX` limit (256KB+ on macOS/Linux) is large enough for any realistic number of files.

**`get_file_at_commit(filepath, commit, workspace_dir) -> bytes | None`**
Runs `git show <commit>:<filepath>` to retrieve a file at a specific commit. Returns `None` if the file doesn't exist
at that commit. Takes a commit parameter (e.g. `HEAD`, `HEAD~1`, a SHA) so it can be used to get both the user's
version and the base version.

**`merge_file(current, base, other, filepath, workspace_dir) -> bool`**
Wraps `git merge-file`. Writes the three versions to temp files, runs the merge, writes the result to disk. Returns
`True` if clean, `False` if conflicts.

### Step 3: Add `compute_local_md5(filepath) -> str` to a utility module

Uses `hashlib.md5` to compute the MD5 of a local file. Returns the hex digest string. Both `hashlib` and `md5` are
standard library, no new dependencies needed.

### Step 4: Extract clobber resolution into a reusable function

The current code does two sync passes: first to `last_synced_cl`, then to the target CL. Perforce restores the
read-only flag when force syncing, so files resolved in the first pass won't clobber again on the second. However,
clobber errors can still occur on the second pass if different files have local changes - for example, the user
manually edited a file on `main` and committed it to git, and that file also changed between `last_synced_cl` and
the target CL.

The clobber resolution logic needs to run independently after each sync pass. Extract it into a function:

```
def resolve_writable_files(writable_files, changelist, workspace_dir,
                           known_case2_files) -> WritableResolution:
    """Classify and force-sync writable files after a p4 sync.

    known_changed: files already identified as writable-changed from a previous sync pass.
    These are force-synced without re-checking (we already saved the user's version).
    """
    # Split into ignored vs tracked
    ignored = get_ignored_files(writable_files, workspace_dir)
    tracked = [f for f in writable_files if f not in ignored]

    # Files already known as writable-changed from first pass - just force sync
    already_known = [f for f in tracked if f in known_changed]
    new_files = [f for f in tracked if f not in known_changed]

    # Classify new files via MD5 comparison
    digests = p4_fstat_digests(new_files, changelist, workspace_dir)
    writable_unchanged, writable_changed = [], []
    for f in new_files:
        local_md5 = compute_local_md5(f)
        if local_md5 == digests.get(f):
            writable_unchanged.append(f)
        else:
            writable_changed.append(f)

    # Force sync all tracked writable files
    for f in writable_unchanged + writable_changed + already_known:
        p4_force_sync_file(changelist, f, workspace_dir)

    return WritableResolution(writable_unchanged, writable_changed, ignored)
```

### Step 5: Rewrite `sync_command()` in `sync.py`

The main command function orchestrates both sync passes and the post-commit merge:

```
def sync_command(args):
    ... existing precondition checks (git clean, p4 clean) ...
    ... find last_synced_cl, determine target changelist ...

    # Before syncing, remember the current HEAD commit (user's state)
    user_commit = git rev-parse HEAD

    # --- First sync pass: to last_synced_cl ---
    result1 = p4_sync(last_synced_cl, ...)
    changed_files = set()
    if result1 has clobber errors:
        resolution1 = resolve_writable_files(
            writable_files, last_synced_cl, workspace_dir,
            known_changed=set())
        changed_files = set(resolution1.writable_changed)
        # Log the resolve section

    # --- Second sync pass: to target CL ---
    result2 = p4_sync(target_changelist, ...)
    if result2 has clobber errors:
        resolution2 = resolve_writable_files(
            writable_files, target_changelist, workspace_dir,
            known_changed=changed_files)
        changed_files |= set(resolution2.writable_changed)
        # Log the resolve section

    # --- Commit ---
    git add + commit (pure Perforce state at target CL)

    # --- Post-commit: merge writable-changed files ---
    # The sync commit is now HEAD. The user's state is at user_commit.
    # The base (common ancestor) is the previous sync commit = user_commit~1
    # (or more precisely, the last commit with a "git-p4son: p4 sync" message).
    for filepath in changed_files:
        base    = git show <last-sync-commit>:<filepath>   # what p4 had before
        theirs  = file on disk                              # Perforce at target CL
        ours    = git show <user_commit>:<filepath>         # user's version
        -> three-way merge, write result to disk

    # Report writable-changed files (merged, conflicts, binary, add/delete asymmetry)
```

**Getting the three versions for merge**:
- **base**: the file from the last sync commit in git. This is the version both the user and Perforce diverged from.
  Retrieve via `git show <last-sync-commit>:<path>` where `last-sync-commit` is the commit before `user_commit`
  (i.e. the most recent commit with a `git-p4son: p4 sync` subject).
- **theirs**: the file on disk after both syncs complete - the Perforce version at the target CL.
- **ours**: the user's version from `git show <user_commit>:<path>`, saved before any syncing.

**Binary files**: Include `-T digest,clientFile,headType` in the `p4 fstat` call to get the file type. Files with a
type starting with `binary` (e.g. `binary`, `binary+l`) must not be three-way merged. Instead, write the user's local
version back to disk after the sync commit so it shows up in `git status`. Report binary files separately from merged
text files in the output so the user knows these need manual attention.

**Handling clobber across two passes**: Perforce restores read-only flags on force sync, so files resolved in the
first pass typically won't clobber again. However, different files may clobber on each pass if the user committed
changes to `main` that overlap with files changed between `last_synced_cl` and the target CL. The `known_changed`
parameter ensures any files already identified as writable-changed are force-synced without redundant classification.
New writable-changed files discovered in the second pass are added to the set. All writable-changed files are merged
once after the commit.

### Step 6: Remove clobber check from `init.py`

Delete the "Checking clobber flag" section (lines 137-146). The `clobber` property on `P4ClientSpec` can stay - it's
not harmful and might be useful elsewhere.

### Step 7: Update `--force` flag in `cli.py`

Change the help text to only mention syncing to older changelists:

```python
sync_parser.add_argument(
    '-f', '--force',
    action='store_true',
    help='Allow syncing to changelists older than the current one.'
)
```

Remove the clobber references from the help text.

### Step 8: Update README.md

- Remove step 1 ("Enable clobber") from the Setup section
- Remove the "Why clobber?" section entirely. Replace it with a new section that documents how p4son handles
  conflicts between locally committed files and upstream Perforce changes. Cover all categories: writable-unchanged (auto
  force-synced), writable-changed text files (three-way merged), writable-changed binary files (local version
  preserved), ignored (skipped with warning), added-local-deleted-upstream, and deleted-local-added-upstream.
- Update the `--force` flag description under the sync command
- Update the `init` command description to remove the clobber mention

### Step 9: Tests

The project uses `unittest` with `unittest.mock`. Tests mock out subprocess calls (`run`, `run_with_output`) and
verify behavior through return values and call assertions. Follow the existing patterns in `tests/`.

**`tests/test_perforce.py`** - new tests for `p4_fstat_digests`:
- Parses multi-record ztag output with `digest` and `clientFile` fields into a `{local_path: md5}` dict
- Handles files that don't exist in Perforce at the given CL (no digest field)
- Empty input returns empty dict

**`tests/test_sync.py`** - new tests for `resolve_writable_files`:
- All writable-unchanged: all files have matching MD5s, all get force-synced, returns empty writable_changed list
- All writable-changed: all files have mismatching MD5s, all get force-synced, returns full writable_changed list
- Mixed: correct classification of each file
- Ignored files: split out correctly, not force-synced, returned in ignored list
- `known_changed` parameter: files in this set are force-synced without MD5 check, not re-added to writable_changed

**`tests/test_sync.py`** - new tests for `compute_local_md5`:
- Returns correct hex digest for a known file content
- Matches the format returned by `p4 fstat -T digest` (32-char lowercase hex)

**`tests/test_git.py`** (new file) - tests for new git helpers:
- `get_ignored_files`: returns correct set when some paths match, returns empty set when none match, handles
  `RunError` from git (exit code 1 = no matches)
- `get_file_at_commit`: returns file content for existing file, returns `None` for non-existent file at commit
- `merge_file`: returns `True` on clean merge, returns `False` when conflicts exist, writes correct content to disk

**`tests/test_sync.py`** - updated tests for `sync_command`:
- Existing tests need updating since `p4_sync` signature changes (no more `force` parameter for clobber)
- New test: sync with writable-unchanged files - verifies force sync happens and files are included in commit
- New test: sync with writable-changed files - verifies force sync, commit with p4 state, then merge-file called
  with correct base/theirs/ours, merged content written to disk
- New test: sync with writable-changed binary file - verifies no merge attempted, user's version restored to disk
- New test: sync with ignored writable files - verifies warning logged, files not force-synced
- New test: clobber errors on both sync passes - verifies writable-changed files from first pass are tracked through
  second pass via `known_changed`
- New test: added-local-deleted-upstream - user's version restored, warning logged
- New test: deleted-local-added-upstream - p4 version kept in commit, warning logged

**`tests/test_init.py`** - update existing tests:
- Remove `test_no_clobber` test (clobber check is removed)
- Update `_MOCK_SPEC` fixtures if needed (clobber field no longer required for init to succeed)

### Step 10: Update CHANGELOG.md

Add an entry for the new behavior.

### Example output

**Normal sync (no writable files)**:
```
# Finding depot root
[ok] //my-workspace

# Checking git workspace
[ok] clean

# Checking p4 workspace
[ok] Clean

# Finding last synced changelist
[ok] CL 12300

# Syncing to last synced CL (12300)
[ok] synced 0 files

# Syncing to latest CL (12345)
[ok] synced 42 files (add: 3, upd: 38, del: 1)

# Committing git changes
[ok] Committed 42 files
```

**Sync with writable files, all writable-unchanged (common case after branch switch)**:
```
...
# Syncing to last synced CL (12300)
synced 35 files (add: 2, upd: 35, clb: 8)

# Resolving 8 writable files
[ok] 8 files unchanged, force synced
path/to/file1
path/to/file2
path/to/file3
path/to/file4
path/to/file5
path/to/file6
path/to/file7
path/to/file8

# Syncing to latest CL (12345)
[ok] synced 12 files (add: 1, upd: 11)

# Committing git changes
[ok] Committed 55 files
```

**Sync with writable files, mixed cases**:
```
...
# Syncing to last synced CL (12300)
synced 35 files (add: 2, upd: 35, clb: 5)

# Resolving 5 writable files
[ok] 3 unchanged files, force synced
path/to/1st/file
path/to/2nd/file
path/to/3rd/file
[warn] 1 file has local changes, will merge after sync
[warn] 1 file ignored by git, skipping
path/to/ignored/file

# Syncing to latest CL (12345)
[ok] synced 12 files (add: 1, upd: 11)

# Committing git changes
[ok] Committed 50 files

# Merging local changes
[ok] 1 file merged successfully
src/engine/renderer.cpp
```

**Sync with merge conflicts**:
```
...
# Resolving 3 writable files
[ok] 1 file unchanged, force synced
src/engine/utils.h
[warn] 2 files have local changes, will merge after sync

# Syncing to latest CL (12345)
[ok] synced 12 files (add: 1, upd: 11)

# Committing git changes
[ok] Committed 48 files

# Merging local changes
[ok] 1 file merged successfully
src/engine/renderer.cpp

[warn] 1 file merged with conflicts
src/engine/physics.cpp

Manually review changes, resolve conflicts and commit when ready.
```

**Sync with binary files**:
```
...
# Resolving 3 writable files
[ok] 1 file unchanged, force synced
src/engine/utils.h
[warn] 2 files have local changes, will merge after sync

# Merging local changes
[ok] 1 file merged successfully
src/engine/renderer.cpp

[warn] 1 binary file has local changes, local version restored
assets/textures/hero.png

Manually review changes and commit when ready.
```

**Sync with add/delete asymmetry**:
```
...
# Resolving 3 writable files
[ok] 1 file unchanged, force synced
src/engine/utils.h
[warn] 2 files have local changes, will merge after sync

# Merging local changes
[warn] 1 file deleted in Perforce but modified locally, local version restored
src/old_module.cpp
[warn] 1 file deleted locally but modified in Perforce
src/new_feature.h

Manually review changes and commit when ready.
```

# Manual testing after implementation

## Bug 1 - File added both upstream in p4 and locally in git.

I made a test where I first added a file in perforce, and submitted it (changelist 9).
I then made sure to sync back my workspace where the file did not exist (`p4 sync //...@8`
Then I changed the file a bit, so that it would cause merge conflicts with
p4, and then committed it to git (hash bb6d7dfc).
When I synced with git p4son it detected that the files changed, claimed that the files
would be merged afterwards but the file was not left unstaged after the sync command was done.
My workspace do not have the clobber flag set

### git log output
```sh
➜  ue-playground-main git:(main) git log --oneline -3
ca70e8f4 (HEAD -> main) git-p4son: p4 sync //ue-playground-main-andreas-luxon/...@9
bb6d7dfc Add test.cpp
ba99d9d6 git-p4son: p4 sync //...@8
```

### File submitted to p4
```sh
> p4 print test.cpp
//projects/ue-playground/main/test.cpp#1 - add change 9 (text)
#include <iostream>

int main(int argc, char* argv[])
{
	using namespace std;
	cout << "Hello world";
}
```

### File commited to git
```diff
➜  ue-playground-main git:(main) git show -p bb6d7dfc
commit bb6d7dfcccc09545bf7e1900d0237da6ec75a6ef
Author: Andreas Andersson <andreas@neoboid.com>
Date:   Tue Mar 31 12:19:40 2026 +0200

    Add test.cpp

diff --git a/test.cpp b/test.cpp
new file mode 100644
index 00000000..4d60a066
--- /dev/null
+++ b/test.cpp
@@ -0,0 +1,7 @@
+#include <iostream>
+
+int main(int argc, char* argv[])
+{
+       using namespace std;
+       cout << "Hello world, eller";
+}
```

### File submitted by `git p4son sync`
```diff
➜  ue-playground-main git:(main) git show -p ca70e8f4
commit ca70e8f42022edf4cf2df2c184fceae16e0843b4 (HEAD -> main)
Author: Andreas Andersson <andreas@neoboid.com>
Date:   Tue Mar 31 12:22:06 2026 +0200

    git-p4son: p4 sync //ue-playground-main-andreas-luxon/...@9

diff --git a/test.cpp b/test.cpp
index 4d60a066..bb732e93 100644
--- a/test.cpp
+++ b/test.cpp
@@ -3,5 +3,5 @@
 int main(int argc, char* argv[])
 {
        using namespace std;
-       cout << "Hello world, eller";
+       cout << "Hello world";
 }
 ```

### Full `git p4son sync` output - when ca70e8f4 was committed
```sh
➜  ue-playground-main git:(main) git p4son sync
# Finding workspace directory
[ok] /Users/andreas/perforce/projects/ue-playground-main

# Finding depot root
[ok] //ue-playground-main-andreas-luxon

# Checking git workspace
>  git status --porcelain
[ok] clean

# Checking p4 workspace
>  p4 -ztag opened //ue-playground-main-andreas-luxon/...
[ok] Clean

# Finding last synced changelist
>  git log -1 --pretty=%s "--grep=: p4 sync //"
[ok] CL 8

# Finding latest changelist
>  p4 -ztag changes -m1 -s submitted //ue-playground-main-andreas-luxon/...#head
[ok] CL 9
>  git rev-parse HEAD
>  git log -1 --pretty=%H "--grep=: p4 sync //"

# Syncing to last synced CL (8)
>  p4 sync //ue-playground-main-andreas-luxon/...@8 /all files up to date
>  p4 sync //ue-playground-main-andreas-luxon/...@8
elapsed: 0:00:00.305177
[ok] synced 0 files

# Syncing to latest CL (9)
>  p4 sync //ue-playground-main-andreas-luxon/...@9
synced 0 files (add: 1, clb: 1)
>  git check-ignore /Users/andreas/perforce/projects/ue-playground-main/test.cpp
>  p4 -ztag fstat -T digest,clientFile,headType /Users/andreas/perforce/projects/ue-playground-main/test.cpp@9
>  p4 sync -f /Users/andreas/perforce/projects/ue-playground-main/test.cpp@9
synced 1 files (add: 1)
elapsed: 0:00:00.015501

# Resolving 1 writable files
[warn] 1 file has local changes, will merge after sync

# Committing git changes
>  git status --porcelain
>  git add .
>  git commit -m "git-p4son: p4 sync //ue-playground-main-andreas-luxon/...@9" --allow-empty
[ok] Committed 1 files

# Merging local changes
[warn] 1 file deleted locally but modified in Perforce
/Users/andreas/perforce/projects/ue-playground-main/test.cpp
```

### Git status after sync command was done
```sh
Manually review changes and commit when ready.
➜  ue-playground-main git:(main) git status
On branch main
nothing to commit, working tree clean
```

## Bug 1 - Manual test attempt after fix attempt 1

It seems like we need to remove the write flag on the file before doing the three way merge.


### Reset p4 and git state to state where bug can be reproduced
```sh
➜  ue-playground-main git:(main) git reset --hard bb6d7dfc
HEAD is now at bb6d7dfc Add test.cpp
➜  ue-playground-main git:(main) p4 sync -f ./test.cpp@8
//projects/ue-playground/main/test.cpp#1 - deleted as /Users/andreas/perforce/projects/ue-playground-main/test.cpp
➜  ue-playground-main git:(main) ✗ git restore .
➜  ue-playground-main git:(main) ls -l test.cpp
-rw-r--r--  1 andreas  staff  111 Mar 31 12:55 test.cpp
➜  ue-playground-main git:(main) cat test.cpp
#include <iostream>

int main(int argc, char* argv[])
{
	using namespace std;
	cout << "Hello world, eller";
}
```

### git log showing state reset
```sh
➜  ue-playground-main git:(main) git log --oneline -2
bb6d7dfc (HEAD -> main) Add test.cpp
ba99d9d6 git-p4son: p4 sync //...@8
```

### git p4son sync
```sh
➜  ue-playground-main git:(main) git p4son sync
# Finding workspace directory
[ok] /Users/andreas/perforce/projects/ue-playground-main

# Finding depot root
[ok] //ue-playground-main-andreas-luxon

# Checking git workspace
>  git status --porcelain
[ok] clean

# Checking p4 workspace
>  p4 -ztag opened //ue-playground-main-andreas-luxon/...
[ok] Clean

# Finding last synced changelist
>  git log -1 --pretty=%s "--grep=: p4 sync //"
[ok] CL 8

# Finding latest changelist
>  p4 -ztag changes -m1 -s submitted //ue-playground-main-andreas-luxon/...#head
[ok] CL 9
>  git rev-parse HEAD
>  git log -1 --pretty=%H "--grep=: p4 sync //"

# Syncing to last synced CL (8)
>  p4 sync //ue-playground-main-andreas-luxon/...@8 /all files up to date
>  p4 sync //ue-playground-main-andreas-luxon/...@8
elapsed: 0:00:00.309340
[ok] synced 0 files

# Syncing to latest CL (9)
>  p4 sync //ue-playground-main-andreas-luxon/...@9
synced 0 files (add: 1, clb: 1)
>  git check-ignore /Users/andreas/perforce/projects/ue-playground-main/test.cpp
>  p4 -ztag fstat -T digest,clientFile,headType /Users/andreas/perforce/projects/ue-playground-main/test.cpp@9
>  p4 sync -f /Users/andreas/perforce/projects/ue-playground-main/test.cpp@9
synced 1 files (add: 1)
elapsed: 0:00:00.014785

# Resolving 1 writable files
[warn] 1 file has local changes, will merge after sync

# Committing git changes
>  git status --porcelain
>  git add .
>  git commit -m "git-p4son: p4 sync //ue-playground-main-andreas-luxon/...@9" --allow-empty
[ok] Committed 1 files

# Merging local changes
[err] [Errno 13] Permission denied: '/Users/andreas/perforce/projects/ue-playground-main/test.cpp'
```

# Bug 2 - File deleted upstream in p4, local change in git

Now I tried deleting a file in p4, I still had local changes to the file to resolve.
The correct thing would have been if the deleted file showed up as untracked in git afterwards
, the contents of the file should be just like it was in the last local git commit that touched
the file (65eeda21)

### Describe CL 10

```powershell
➜  ue-playground-main git:(main) p4 describe 10
Change 10 by andreas@ue-playground-main-andreas-luxon on 2026/03/31 16:35:48

	Remove test.cpp again

Affected files ...

... //projects/ue-playground/main/test.cpp#2 delete

Differences ...
```

### Git status
```sh
➜  ue-playground-main git:(main) git log --oneline -3
65eeda21 (HEAD -> main) Re-apply test.cpp local changes
54eb5733 git-p4son: p4 sync //ue-playground-main-andreas-luxon/...@9
bb6d7dfc Add test.cpp
```

### local change in HEAD/65eeda21
```diff
➜  ue-playground-main git:(main) git show -p 65eeda21
commit 65eeda210608eb7f67505e6a3cf666a92eb7f4f0 (HEAD -> main)
Author: Andreas Andersson <andreas@neoboid.com>
Date:   Tue Mar 31 13:06:43 2026 +0200

    Re-apply test.cpp local changes

diff --git a/test.cpp b/test.cpp
index bb732e93..4d60a066 100644
--- a/test.cpp
+++ b/test.cpp
@@ -3,5 +3,5 @@
 int main(int argc, char* argv[])
 {
        using namespace std;
-       cout << "Hello world";
+       cout << "Hello world, eller";
 }
```

### Write flag set
```sh
➜  ue-playground-main git:(main) ls -l test.cpp
-rw-r--r--  1 andreas  staff  111 Mar 31 16:38 test.cpp
```


### git p4son sync output with error at end
```sh
➜  ue-playground-main git:(main) git p4son sync
# Finding workspace directory
[ok] /Users/andreas/perforce/projects/ue-playground-main

# Finding depot root
[ok] //ue-playground-main-andreas-luxon

# Checking git workspace
>  git status --porcelain
[ok] clean

# Checking p4 workspace
>  p4 -ztag opened //ue-playground-main-andreas-luxon/...
[ok] Clean

# Finding last synced changelist
>  git log -1 --pretty=%s "--grep=: p4 sync //"
[ok] CL 9

# Finding latest changelist
>  p4 -ztag changes -m1 -s submitted //ue-playground-main-andreas-luxon/...#head
[ok] CL 10
>  git rev-parse HEAD
>  git log -1 --pretty=%H "--grep=: p4 sync //"

# Syncing to last synced CL (9)
>  p4 sync //ue-playground-main-andreas-luxon/...@9 /all files up to date
>  p4 sync //ue-playground-main-andreas-luxon/...@9
elapsed: 0:00:00.310103
[ok] synced 0 files

# Syncing to latest CL (10)
>  p4 sync //ue-playground-main-andreas-luxon/...@10
synced -1 files (del: 1, clb: 1)
>  git check-ignore /Users/andreas/perforce/projects/ue-playground-main/test.cpp
>  p4 -ztag fstat -T digest,clientFile,headType /Users/andreas/perforce/projects/ue-playground-main/test.cpp@10
>  p4 sync -f /Users/andreas/perforce/projects/ue-playground-main/test.cpp@10
synced 0 files (del: 1)
elapsed: 0:00:00.014321

# Resolving 1 writable files
[warn] 1 file has local changes, will merge after sync

# Committing git changes
>  git status --porcelain
>  git add .
>  git commit -m "git-p4son: p4 sync //ue-playground-main-andreas-luxon/...@10" --allow-empty
[ok] Committed 1 files

# Merging local changes
[err] [Errno 2] No such file or directory: '/Users/andreas/perforce/projects/ue-playground-main/test.cpp'
➜  ue-playground-main git:(main)
```

### git status shows no changes to stage
```sh
➜  ue-playground-main git:(main) git status
On branch main
nothing to commit, working tree clean
```

### sync commit shows removed file

```diff
➜  ue-playground-main git:(main) git show HEAD
commit 972b80a7dc1d6b576fef9e9126a072d19aaa6d29 (HEAD -> main)
Author: Andreas Andersson <andreas@neoboid.com>
Date:   Tue Mar 31 16:41:08 2026 +0200

    git-p4son: p4 sync //ue-playground-main-andreas-luxon/...@10

diff --git a/test.cpp b/test.cpp
deleted file mode 100644
index 4d60a066..00000000
--- a/test.cpp
+++ /dev/null
@@ -1,7 +0,0 @@
-#include <iostream>
-
-int main(int argc, char* argv[])
-{
-       using namespace std;
-       cout << "Hello world, eller";
-}
```
