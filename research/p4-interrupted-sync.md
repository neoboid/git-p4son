# What p4 leaves behind when a sync is interrupted

Research for issue #293 (map #291, feature #21). Date: 2026-11-16.

Sources are marked **[observed]** (experiment against a scratch server, commands given) or **[docs]** (Perforce
documentation, URL given). Anything else is marked **[inferred]**.

## Setup

- Client `p4` and server `p4d`: `Rev. P4/MACOSX12ARM64/2025.1/2810567 (2025/08/05)` and
  `Rev. P4D/MACOSX12ARM64/2025.1/2810567 (2025/08/05)`, macOS 26 (Darwin 25.6) on arm64, local APFS disk.
- Scratch server: `p4d -r <scratch>/root -p localhost:1777 -J off -d`, case-insensitive, no license, default
  configurables (no `net.parallel.*`, so sync is single-threaded).
- Every command ran with `P4PORT=localhost:1777 P4USER=tester P4CONFIG= P4TICKETS=<scratch>/tickets`, client `ws`
  (and `ws2` for option and rename tests), view `//depot/... //ws/...`.
- Depot:
  - Change 1 adds `01_big.bin` to `06_big.bin` (300 MiB of random data each, `binary+F`), `02b_del.txt`,
    `04b_small.txt`, `05b_del.txt`, `07_small.txt`.
  - Change 2 edits all six big files and both small text files, adds `03b_newbig.bin` (300 MiB) and `08_new.txt`,
    and deletes `02b_del.txt` and `05b_del.txt`.
- A full `p4 sync //depot/...@2` from `@1` takes about 5.3 s.
- Harness (a Python script) for each run:
  1. Resets the workspace to `@1` (`p4 sync -q //depot/...@1`, removing files not in change 1).
  2. Runs `p4 -L en sync //depot/...@2` with stdout and stderr on pipes, as git-p4son does.
  3. Polls the workspace directory every 5 ms and sends one signal when a trigger fires: a time delay, a file
     reaching a size, or a file landing (a new inode at full size).
  4. Records the exit code, the directory, `p4 have`, `p4 diff -se` (unopened files differing from the have
     revision), `p4 diff -sd` (have-list files missing on disk), `p4 sync -n //depot/...@2`, then
     `p4 sync //depot/...@2`, and finally checks every file's MD5 against change 2.
- Signals: SIGINT, SIGTERM and SIGKILL, sent to p4 alone (not its process group). Targeted runs (mid-add, mid-update,
  right after a file lands) were repeated two or three times per signal. 24 more runs used random delays across the
  whole sync (8 per signal).

## How p4 writes files

**[observed]** From a 5 ms directory trace of an uninterrupted sync:

- Deletes run first, then the files are transferred in an order that is not the order of the output lines (here:
  `03b_newbig.bin`, `04b_small.txt`, `07_small.txt`, `08_new.txt`, then `01_big.bin` to `06_big.bin`).
- An **update** is written to a temporary file in the same directory, named `tmp.<pid>.<hex>.<n>` (mode 0644 while
  written), then renamed over the target. The target keeps its old content until the rename.
- An **add** is written **directly to the target path** (mode 0644 while written, made read-only at the end). No
  temporary file is used.
- On a pipe, stdout is block-buffered: in an uninterrupted run all 12 output lines arrived together when p4 exited,
  not as files were transferred. A line written to stderr flushed stdout with it (see the failed rename below).

## Q1: Ctrl-C or a kill mid-transfer

### File in the middle of an update

**[observed]** Trigger `tmpsize:100000000` (a `tmp.*` file reaching 100 MB), 1 run per signal, plus random runs that
landed mid-update (INT 6, TERM 5, KILL 6):

| Signal | p4 exit | Have-list for the file | Target on disk | Temporary file |
| --- | --- | --- | --- | --- |
| SIGINT | 255 | old revision | old content, intact | removed |
| SIGTERM | -15 | old revision | old content, intact | left behind, partial |
| SIGKILL | -9 | old revision | old content, intact | left behind, partial |

### File in the middle of an add

**[observed]** Trigger `size:03b_newbig.bin=100000000`, 1 run per signal, plus random runs (TERM-1, TERM-8):

| Signal | p4 exit | Have-list for the file | On disk |
| --- | --- | --- | --- |
| SIGINT | 255 | not listed | removed |
| SIGTERM | -15 | not listed | partial file at the real path, writable (0644) |
| SIGKILL | -9 | not listed | partial file at the real path, writable (0644) |

### Files finished before the interrupt

**[observed]** Every file finished well before the signal was in the have-list at its new revision. Deleted files were
gone from disk and from the have-list. In every run, `p4 diff -se` and `p4 diff -sd` printed nothing: no file in the
have-list differed from, or was missing from, its recorded revision.

### The landing window: on disk but not yet recorded

**[observed]** The have-list is updated a little after a file lands, not atomically with it. Trigger `landed:<file>`
(the file has a new inode at full size), so the signal lands just after the rename or the last write:

| Signal | File | Runs | Have-list afterwards |
| --- | --- | --- | --- |
| SIGKILL | `03b_newbig.bin` (add) | 2 (+2 random: KILL-1, KILL-8) | not listed in all 4 |
| SIGKILL | `01_big.bin` (update) | 2 | `#2` once, `#1` once |
| SIGKILL | `04b_small.txt` (update) | 2 | `#1` in both |
| SIGTERM | `03b_newbig.bin` (add) | 2 | not listed in both |
| SIGTERM | `01_big.bin` (update) | 2 | `#2` once, `#1` once |
| SIGINT | `03b_newbig.bin` (add) | 2 | `#1` in both |
| SIGINT | `01_big.bin` (update) | 2 | `#2` in both |

In every case the file on disk had the new content, complete and read-only. So after SIGTERM or SIGKILL the disk can be
one file **ahead** of the have-list: complete new content recorded at the old revision, or an add not recorded at all.

### SIGINT is handled, SIGTERM and SIGKILL are not

**[observed]** p4 catches SIGINT: it exits 255 within a few milliseconds, removes the file it was writing (temporary
or added), and records a file that had already landed. No SIGINT run left anything behind. SIGTERM behaved exactly like
SIGKILL in every run: the process dies at once and leaves the partial file and the have-list as they were.

**[inferred]** git-p4son's `_terminate` (SIGTERM, then kill after 5 s) gives the SIGKILL outcome. On a terminal Ctrl-C
on macOS and Linux, p4 is in the foreground process group and gets the SIGINT itself, which it handles before
git-p4son's SIGTERM arrives. On Windows, `Popen.terminate()` is `TerminateProcess`, which is a hard kill like SIGKILL;
how p4 on Windows reacts to a console Ctrl-C was not tested.

## Q2: `rename: failed to rename`

### The approximation used on macOS

On Windows, p4 reports `rename: failed to rename <path> after 10 attempts: Cannot create a file when that file already
exists.` (issue #21) when another process holds the target open. macOS has no mandatory file locks, so the failure had
to be simulated.

**[observed]** Approximations that do not work:

- `chflags uchg` (user immutable flag) on the target: p4 2025.1 clears the flag and replaces the file, without an
  error. The flag was gone afterwards.
- A read-only parent directory: p4 cannot create the temporary file there either, so the failure is at open, not at
  rename.

**[observed]** What was used: a copy of the p4 binary, re-signed ad hoc so that macOS allows `DYLD_INSERT_LIBRARIES`,
with a small library interposing `rename()` to fail with `EBUSY` when the destination contains `$FAIL_RENAME`:

```c
int my_rename(const char *a, const char *b) {
    if (match(b)) { errno = EBUSY; return -1; }
    return rename(a, b);
}
```

```sh
cp /usr/local/bin/p4 bin/p4 && codesign --remove-signature bin/p4 && codesign -s - bin/p4
clang -dynamiclib -o bin/failrename.dylib failrename.c
FAIL_RENAME=02_big.bin DYLD_INSERT_LIBRARIES=bin/failrename.dylib bin/p4 -L en sync //depot/...@2
```

How faithful this is:

- It fails the same system call p4 uses to put the temporary file in place, after the whole file was transferred,
  which is where the Windows failure happens.
- The message differs: on macOS p4 prints `rename: <path>: Resource busy` once, with no retries, so it does **not**
  match git-p4son's `rename: failed to rename ` prefix. The Windows "after 10 attempts" text comes from a Windows-only
  retry loop that this cannot exercise.
- **[inferred]** "Cannot create a file when that file already exists" (`ERROR_ALREADY_EXISTS`) suggests that on
  Windows p4 deletes the target and then moves the temporary file. Deleting a file another process holds open with
  delete sharing leaves it "delete pending": the name stays until the other process closes it, then the file vanishes.
  If so, the Windows target could disappear later, leaving a have-list entry with no file. The interposer cannot show
  this, and whether the temporary file is cleaned up on Windows is also unknown.

### Result

**[observed]** Without killing p4 (`FAIL_RENAME=02_big.bin`):

- p4 reported the failure and **carried on** with the rest of the sync. It exited 1.
- `02_big.bin` stayed in the have-list at `#1`, with the old content intact on disk. The temporary file was removed.
- Every other file reached change 2 and was recorded.
- `p4 sync -n //depot/...@2` listed exactly `//depot/02_big.bin#2 - updating ...`. `p4 diff -se` printed nothing.

**[observed]** Mirroring git-p4son (stream both pipes, SIGTERM p4 on the first line starting with `rename:`,
`FAIL_RENAME=01_big.bin`):

- The stderr line arrived at 2.628 s together with all of the buffered stdout lines, and p4 died of the SIGTERM
  (exit -15) at 2.630 s.
- `01_big.bin` was at `#1` in the have-list with its old content, and every file not yet transferred was at its old
  revision. The 4 files done before it (`03b_newbig.bin`, `04b_small.txt`, `07_small.txt`, `08_new.txt`) were recorded
  at their new revisions.
- No temporary file was left.
- `p4 sync -n` listed the 6 big files, and a plain `p4 sync //depot/...@2` then brought every file to change 2.

## Q3: does `p4 sync <root>/...@N` transfer only what is missing?

**[observed]** Yes, in every run, judged by the have-list: the resync listed only files whose have-list revision was
not the target. It did not re-transfer files already recorded at the target. In the default `noallwrite noclobber`
client it finished every interrupted state correctly, except one:

- **A partial add left by SIGTERM or SIGKILL** blocks the resync: `Can't clobber writable file <path>`, p4 exits 1,
  and the partial file stays. The file is not in the have-list and is writable, so `noclobber` protects it as if it
  were the user's (seen in out-add-TERM, out-add-KILL, TERM-1 and TERM-8).
- A leftover `tmp.*` file is ignored by the resync and stays in the workspace, as an untracked file.
- A file in the landing window (complete and read-only, have-list behind) is transferred again and recorded. That
  costs one extra transfer, nothing more.
- A file deleted from disk with the delete not yet recorded is handled: simulated by deleting `02b_del.txt` by hand at
  `@1`, `p4 diff -sd` listed it, and `p4 sync @2` reported `deleted as` and dropped it from the have-list, exit 0.

## Q4: does `p4 sync -n` list exactly what is left?

**[observed]** In all 48 interrupt runs (both client options), the list from `p4 sync -n //depot/...@2` was
identical, line for line, to the list the real resync printed. It was exactly the files whose have-list entry was not
the target revision.

It lists what the have-list says is left, not what the disk says:

- A file in the landing window is listed although its content is already the target.
- A leftover `tmp.*` file is not mentioned.
- With `noclobber` and `noallwrite`, a partial add is listed as a plain `added as`. The `Can't clobber writable file`
  error appears only in the real sync.
- With `allwrite` the preview does show the problem lines (`can't overwrite existing file`, `can't update modified
  file`), the same as the real sync.

## Q5: client options, adds and deletes

### Adds and deletes

**[observed]** Adds are the risky case. They are written in place, so a SIGTERM or SIGKILL mid-add leaves a partial
file at the real path, outside the have-list, and that file blocks the next sync under `noclobber` (above).

Deletes run first and are quick (13 ms for two files here). No interrupt landed mid-delete. A delete done on disk but
not recorded is finished by the next sync.

### `allwrite` (with `noclobber`)

**[docs]** "If `allwrite` is set, unopened files in the workspace are left writable." "If `allwrite` is set and
`noclobber` is NOT specified, a safe synchronization is performed." The page does not say what a safe sync does with
writable files. https://help.perforce.com/helix-core/server-apps/cmdref/current/Content/CmdRef/p4_client.html

**[observed]** With `allwrite noclobber` on client `ws2`, every synced file is writable, and p4 checks writable files
against their have revision before replacing them. A sync from `@1` took about 4.4 s before the first transfer,
against 1.5 s without `allwrite`; **[inferred]** this is the time to read the existing files for that check.

- **Partial add** (SIGKILL mid-add, observed once): `p4 sync -n` and `p4 sync` both print
  `//depot/03b_newbig.bin#1 - can't overwrite existing file <path>`. The sync **exits 0** and the partial file stays.
- **Landing window on an update** (SIGKILL just after the file landed, observed: `04b_small.txt` 2 of 2 runs,
  `01_big.bin` 1 of 2 runs): the file has the new content but the have-list says `#1`. `p4 sync -n` and `p4 sync`
  print `//depot/<file>#2 - can't update modified file <path>`, exit 0, and the have-list **stays at `#1`
  permanently**. A plain sync never fixes it.
  - Simulated as well: content of `#2` copied into `04_big.bin` at `@1` gave the same line.
  - A later `p4 sync -q @1` then left the change 2 content on disk under a `#1` have-list entry, because p4 did not
    consider the file changed.
  - Only `p4 sync -f` (or `p4 sync -k` to record the revision) repairs it.
- **Landing window on an add**, simulated with the complete content of `03b_newbig.bin` placed at `@1`: p4 printed
  `added as` and recorded it. **[inferred]** A complete file that matches is accepted, so only a partial add is
  refused.

**[observed]** git-p4son's `P4SyncOutputProcessor` only knows the `Can't clobber writable file ` prefix, so these two
`allwrite` lines would be logged as `Unparsable line` and the sync would count as a success.

### `clobber`

**[docs]** "If `clobber` is set, a `p4 sync` overwrites ("clobbers") files in the workspace that have the same name
as the newly-synced files if those files are writable but unopened." The default is `noclobber`.
https://help.perforce.com/helix-core/server-apps/cmdref/current/Content/CmdRef/p4_client.html

**[observed]** Simulated partial add (first 100 MB of `03b_newbig.bin`, writable, not in the have-list) under
`noallwrite clobber` and under `allwrite clobber`: the sync overwrote it with `added as`, exit 0, and every file
matched change 2. With `allwrite clobber`, a file with `#2` content and a `#1` have-list entry was also updated and
recorded at `#2`.

### `sync -f` and `sync -k`

**[docs]** `-f`: "If the file is writable, it is overwritten. This option does not affect open files, but it does
override the `noclobber` client option." `-k`: "Keep existing workspace files and update the have list without
updating the client workspace."
https://help.perforce.com/helix-core/server-apps/cmdref/current/Content/CmdRef/p4_sync.html

The documentation does not describe partial transfers, temporary files or when the have-list is written during a
sync. All of that above comes from the experiments.

## Summary

- **Mid-transfer update:** the have-list keeps the old revision and the target keeps its old content. SIGINT removes
  the temporary file; SIGTERM and SIGKILL leave a partial `tmp.<pid>.<hex>.<n>` next to it.
- **Mid-transfer add:** the file is not in the have-list. SIGINT removes the file; SIGTERM and SIGKILL leave a partial,
  writable file at the real path.
- **Just after a file lands:** with SIGTERM or SIGKILL the have-list can still show the old revision (update) or no
  entry (add) while the disk has the complete new file. SIGINT recorded it in every run.
- **Failed rename:** the file stays at the old revision with old content, p4 carries on unless killed, and
  `p4 sync -n` lists it.
- **Resync:** `p4 sync @N` transfers only what the have-list lacks and repairs everything except:
  - a partial add under `noclobber`, which fails with `Can't clobber writable file` (exit 1), or with
    `can't overwrite existing file` (exit 0) under `allwrite`;
  - under `allwrite`, a file in the landing window, which is refused as `can't update modified file` (exit 0) and
    left recorded at the old revision for good.
- **Preview:** `p4 sync -n` lists exactly what the resync will transfer, which is what the have-list lacks. It does
  not see leftover temporary files, and under `noclobber` it does not warn about a partial add.

## Not determined

- Windows behaviour: the real "failed to rename after 10 attempts" path, whether the held-open target disappears
  later, whether temporary files are cleaned up, and how p4 handles a console Ctrl-C or `TerminateProcess`.
- Parallel sync (`net.parallel.max`), where several files are in flight at once.
- A dropped network connection rather than a killed client, and an interrupted server.
- Behaviour on Linux, and with `p4 sync` options other than the defaults git-p4son uses.
