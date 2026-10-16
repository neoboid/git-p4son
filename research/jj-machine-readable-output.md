# jj machine-readable output for changes and file contents

Research for ticket 01 of `wayfinder/jj-first-class`: what a jj backend can read through jj itself instead of git,
how stable it is from jj 0.41 to 0.46 (current, released 2026-10-07), and what only git can give in a colocated repo.

Sources:

- jj docs at v0.46.0 and v0.41.0: `docs/templates.md`, `docs/filesets.md`, `docs/revsets.md`, `docs/config.md`,
  `docs/windows.md`, `docs/git-compatibility.md` (github.com/jj-vcs/jj, also served at jj-vcs.github.io/jj).
- jj source at tag v0.46.0: `cli/src/commands/diff.rs`, `cli/src/diff_util.rs`, `cli/src/commit_templater.rs`,
  `cli/src/commands/file/show.rs`, `cli/src/config/templates.toml`, `lib/src/git_backend.rs`, `lib/src/eol.rs`,
  `lib/src/diff_presentation/mod.rs`, `core/src/backend.rs`, `cli/tests/test_diff_command.rs`.
- jj `CHANGELOG.md` at v0.46.0.
- Live runs of the official macOS aarch64 release binaries 0.41.0 and 0.46.0, on a repo made with
  `jj debug init-simple` (the simple backend; no git and so no rename or copy detection, see the last section).
- git-p4son `git_p4son/git.py` on `main`, and `git_p4son/jj.py` plus the `git.py` diff on `minimal-jj-rebased`.

## Summary

- **Changed files: use `jj diff -T`, not `--summary`.** A `TreeDiffEntry` template gives the status, the
  repo-relative slash path of both sides, file type and executable bit per side, and `json()` escapes paths. Every
  piece of it exists in 0.41 and is unchanged in 0.46. `--summary`, `--types` and `--name-only` print unquoted,
  cwd-relative, platform-separator paths and fold renames into `{a => b}`, so they are for humans.
- **Range semantics differ from `git diff base...rev`.** `jj diff --from A --to B` is a two-tree diff (git's
  `A B`), and `jj diff -r 'base..rev'` diffs from the merged parents of the range's roots. The match for git's
  merge-base diff is `jj diff --from 'fork_point(base | rev)' --to rev`.
- **Renames and copies are on by default and ignore git config.** jj asks gitoxide for rewrites at 50% similarity,
  limit 1000, with copies sourced from the modified files only, and only between regular files. git's result
  depends on the user's `diff.renames`; `git diff` finds no copies by default.
- **File contents: `jj file show -T '' -r REV 'root-file:"PATH"'`, one process per file.** It prints the stored
  bytes with no length framing, so several files in one call cannot be split apart. Symlinks print nothing and
  exit 0, a missing single path exits 1, a missing path inside a union only warns. `git cat-file --batch` has no jj
  equivalent.
- **Blob ids need jj 0.46.** `TreeEntry.normal_value()` (new in 0.46.0) prints the full object id, which with the
  git backend is the git blob OID. On 0.41 to 0.45 jj cannot print a blob id, so `get_blob_oids` stays on git.
- **Only git gives**: batched content and OID reads, symlink targets, the merge driver (`git merge-file`), the
  editor (`git var GIT_EDITOR`), ignore checks against the working tree, and the commits themselves.
- **Stability**: jj documents that `json()` output is "usually stable ... but the backward compatibility isn't
  guaranteed". Every format and method this note relies on predates 0.41 and was identical in 0.41.0 and 0.46.0
  in the live runs, apart from `normal_value()`.

## What git-p4son reads from git today

| `git.py` function | git command | What it gives |
| --- | --- | --- |
| `get_local_changes(base, rev)` | `git diff --name-status -z base...rev` | adds, mods (M, T), dels, moves |
| `get_commit_lines_since`, `..._subjects_since` | `git log --format='%h %s' --reverse --no-merges` | sha, subject |
| `get_head_subject` | `git log -1 --format=%s` | HEAD subject |
| `get_files_at_commits` | `git cat-file --batch` | blob bytes for many (commit, path) pairs in one process |
| `get_blob_oids` | `git cat-file --batch-check` | blob OIDs for many pairs |
| `list_tracked_files(rev)` | `git ls-files -z` or `git ls-tree -r --name-only -z rev` | tracked paths |
| `find_base_commits` | `git log -z --no-renames --name-status --stdin` | last sync commit or add per path, one walk |
| `git_last_synced_changelist` | `git log -1 --grep` | last sync commit subject |
| `get_untracked_files` (jj branch) | `git read-tree` + `git ls-files --others` | files jj did not snapshot |
| `get_dirty_files`, `get_staged_files`, `add_*`, `commit` | index and worktree | no jj counterpart |
| `merge_file`, `resolve_editor` | `git merge-file`, `git var GIT_EDITOR` | no jj counterpart |

`jj.py` on `minimal-jj-rebased` already reads through jj: `jj log -T 'json(self) ++ ...'` for commit id, parents,
description, empty, conflict and divergent; `jj bookmark list -T 'json(self)'`; `jj op log -T id`;
`jj config get working-copy.eol-conversion`; `jj sparse list`. It runs every call with `--ignore-working-copy
--at-op <id>`, and `--at-operation` implies `--ignore-working-copy` (`jj diff --help`, 0.46.0).

## Changed files

### `jj diff` modes and their output

`jj diff` takes `-r REVSETS`, or `--from`/`--to` (each defaults to `@` when the other is given), plus filesets, and
offers `-s/--summary`, `--stat`, `--types`, `--name-only`, `--git`, `--color-words`, `--tool` and `-T/--template`
(`jj diff --help`, identical options in 0.41.0 and 0.46.0). `-T` conflicts with the short and long formats and
`--tool`; with `-T` the renderer gets no formats, so only the template prints (`cli/src/commands/diff.rs`).

From `cli/src/diff_util.rs` (`show_diff_summary`, `show_types`, `show_names`) and the live runs:

- `--summary` prints `<M|A|D|R|C> <path>`. Renames and copies print as `R {file1 => file3}` and
  `C {file2 => file4}` (`cli/tests/test_diff_command.rs`, `test_diff_basic`).
- `--types` prints two letters (before, after) from `-` absent, `F` file, `L` symlink, `C` conflict, `G` submodule,
  then the path in the same `{a => b}` form. A symlink replacing a file shows `FL`. It does not show the executable
  bit.
- `--name-only` prints only the target path.
- All three format paths with `RepoPathUiConverter::format_file_path`: relative to the cwd (run from `sub/`, the
  live output was `D ../big.txt`), with the platform separator, and with no quoting or escaping. A file named
  `tab<TAB>name` printed a raw tab; a newline in a name would split the record. None of these are safe to parse.

### `jj diff -T` with a `TreeDiffEntry` template

The template's `self` is a `TreeDiffEntry`, and its 0-argument methods are keywords (`source`, not `source()`;
`source()` fails with "Function `source` doesn't exist"). Methods (docs/templates.md, same list in 0.41.0 and
0.46.0):

- `path`: target path; `status`: `modified`, `added`, `removed`, `copied` or `renamed`; `status_char`: `M A D C R`;
  `display_diff_path`: the human `{a => b}` form, cwd-relative.
- `source`, `target`: `TreeEntry` values with `path()`, `file_type()` (`file`, `symlink`, `tree`, `git-submodule`,
  `conflict`, and an undocumented empty string for an absent side, `describe_file_type` in
  `cli/src/commit_templater.rs`), `executable()` (false for anything but a resolved executable file),
  `conflict()`, `conflict_side_count()`, and from 0.46.0 `normal_value()`.

A `RepoPath` in a template prints `as_internal_file_string()`: workspace-relative with forward slashes on every
platform, whatever the cwd (`impl Template for RepoPathBuf`; the live run from `sub/` printed `big.txt`). `RepoPath`
is `Serialize`, so `json(path)` gives a JSON string with tabs, quotes and newlines escaped. Double-quoted template
literals support `\0`, `\t` and `\n` (docs/templates.md, `StringLiteral`), so NUL separators work too. jj paths are
always UTF-8 (docs/git-compatibility.md: "Paths are assumed to be UTF-8"), so JSON loses nothing.

A template that maps one-to-one onto `LocalChanges`, checked live on 0.41.0 and 0.46.0:

```
status_char ++ "\t" ++ json(source.path()) ++ "\t" ++ json(path) ++ "\t"
  ++ source.file_type() ++ "/" ++ target.file_type() ++ "\t"
  ++ source.executable() ++ "/" ++ target.executable() ++ "\n"
```

Sample lines: `M	"exe.sh"	"exe.sh"	file/file	false/true` (chmod +x only), `M	"tc.txt"	"tc.txt"	file/symlink	false/false`
(git's `T`), `A	"tab\tname"	"tab\tname"	/file	false/false`.

Mapping to `get_local_changes`: `M` to mods (it covers git's `T` typechange and mode-only changes, since both sides
are present), `A` to adds, `D` to dels, `R` to moves with `(source.path, path)`, `C` to adds of `path` with the source
untouched. There is no separate typechange status; `source.file_type != target.file_type` detects one.

### Range semantics

`cli/src/commands/diff.rs`:

- `--from A --to B` diffs `A`'s tree against `B`'s tree directly, like `git diff A B`. It is not a merge-base diff.
- `-r REVSET` diffs from the merged trees of the parents of the revset's roots to the merged trees of its heads,
  and refuses revsets with gaps ("Cannot diff revsets with gaps in."). For a linear feature stack `-r 'base..rev'`
  equals `git diff base...rev`. They differ when the range holds a merge of the base into the feature: the root's
  parent is the old fork point, so the diff includes base changes that git's merge-base diff leaves out.
- `fork_point(x)` is `heads(::x_1 & ... & ::x_N)` (docs/revsets.md), so the exact counterpart of
  `git diff base...rev` is `jj diff --from 'fork_point(base | rev)' --to rev`. `--from` needs a single revision, so
  this errors where git would warn and pick one of several merge bases.

### Rename and copy detection

Copy records come from the git backend's `get_copy_records` (`lib/src/git_backend.rs`): a gitoxide tree diff with
`track_rewrites` at `percentage: 0.5`, `limit: 1000`, `track_empty: false`, and copies with
`CopySource::FromSetOfModifiedFiles` at 0.5. Records whose source or target is not a blob are dropped ("Renamed
symlinks cannot be returned"). `jj diff` gathers records for every (root parent, head) pair. So:

- jj detects copies by default, sourced only from files the range also modified (jj's test shows `file2` modified
  and `C {file2 => file4}`), like `git diff -C`. `git diff` finds copies only with `diff.renames=copies` or `-C`;
  `get_local_changes` handles `C` but rarely sees it.
- jj ignores git's `diff.renames` and `diff.renameLimit`. git-p4son's `git diff --name-status` does not pin
  `--find-renames`, so today a user with `diff.renames=false` gets add plus delete. jj's result does not depend on
  user config.
- A fileset restriction drops pairing when only one side matches: `jj diff --types 'glob:file[12]'` prints
  `F- file1` for a rename (`test_diff_basic`). Restrict through the template's output instead if renames matter.
- 0.44.0 fixed a merge-commit case: "`jj diff` and `jj status` no longer omit a rename or copy in a merge commit
  when the renamed source is present in more than one parent" (CHANGELOG, jj #9752). On 0.41 to 0.43 such a rename
  shows as add plus delete.
- `Commit.diff()` in a `jj log` template also uses copy records (`TreeDiff::from_commit`), so per-commit file lists
  come with renames. The evolog `diff()` has none ("TODO: copy tracking").

The simple backend used for the live runs has no copy records, so the rename lines above come from jj's own test
snapshots, not from a live run.

## File contents at a revision

`jj file show [-r REV] [-T TEMPLATE] FILESETS...` (`cli/src/commands/file/show.rs`):

- It prints the **stored** bytes, from `materialize_tree_value`. EOL conversion lives only in the local working
  copy (`lib/src/local_working_copy.rs`, `lib/src/eol.rs`), so output matches `git cat-file`. Live: a committed
  `a\r\nb\r\n` printed `a \r \n b \r \n` with no conversion configured.
- `-T` renders a `TreeEntry` header before each file's bytes. The default is `templates.file_show = ''`
  (`cli/src/config/templates.toml`), which a user may override, so a backend should pass `-T ''` explicitly.
- Several files in one call come out in tree order, not argument order (live: `keep.txt` before `mod.txt`), with
  no length or terminator. `TreeEntry` has no size method, so a header cannot frame the bytes. A batch read needs
  one process per file, or git.
- A single missing path fails: `Error: No such path: nope.txt`, exit 1. A missing path inside a union only warns
  (`Warning: No matching entries for paths: nope.txt`) and exits 0, so missing files must be told apart by
  listing first or by one call per file.
- A symlink prints `Warning: Path 'tc.txt' exists but is not a file.` on stderr and nothing on stdout, exit 0
  (0.41.0 omits the full stop). A submodule behaves the same. No jj template exposes a symlink target.
- A conflicted file prints with conflict markers, in the user's `ui.conflict-marker-style`.
- Paths are filesets. A bare argument is a cwd-relative `prefix-glob:`, and a name with meta characters fails to
  parse (live: `pa(ren).txt` gave "Failed to parse fileset: Syntax error", exit 1). Exact repo paths need
  `root-file:"..."`, with `"` and `\` escaped as in template string literals (docs/filesets.md). Live:
  `'root-file:"quo\"te.txt"'` worked. The single-path fast path covers `root-file:` (`get_single_path`).

`jj file list [-r REV] [-T TEMPLATE]` lists a revision's files with a `TreeEntry` template, so
`json(path) ++ " " ++ file_type ++ " " ++ executable ++ "\n"` replaces `git ls-tree -r --name-only` and also gives
symlinks and the executable bit (live on 0.41.0 and 0.46.0). jj has no index, so the `git ls-files` (index) branch
of `list_tracked_files` has no jj counterpart; under jj the tracked set is `jj file list -r @`.

### Object ids

`TreeEntry.normal_value() -> Option<TreeValue>` is new in 0.46.0 (CHANGELOG: "Added the `TreeEntry.normal_value()`
template method and the `TreeValue` type to access resolved tree values, formatted as their full object IDs,
including Git submodule commit IDs"). With the git backend the file id is the git blob OID; the simple backend
printed 128-hex BLAKE2b ids. It is unset for an absent or conflicted side. On 0.41.0 the method does not exist
("Method `normal_value` doesn't exist for type `TreeEntry`"). `--git` diff output carries abbreviated `index` lines
(`index 257cc5642c..0000000000`), which are not full OIDs.

## Executable bit, symlinks, line endings, binary detection

- **Executable bit**: stored per file and readable as `executable()` on any `TreeEntry`. On Unix,
  `working-copy.exec-bit-change` (`respect`, `ignore`, or `auto` by default) decides whether snapshots record it;
  Windows never does (docs/config.md). Present since 0.37.
- **Symlinks**: `file_type()` is `symlink`, `--types` shows `L`. Content and target are unavailable through jj.
  Renamed symlinks never pair up as renames.
- **Line endings**: `working-copy.eol-conversion` (`none`, `input`, `input-output`, since 0.32) converts only on
  snapshot and checkout. jj reads neither `.gitattributes` nor `core.autocrlf` (docs/windows.md,
  docs/git-compatibility.md: ".gitattributes: No"), which is why `minimal-jj-rebased` maps the setting onto
  `git -c core.autocrlf=...` for `git add`. Stored content is the same through jj or git.
- **Binary detection**: two heuristics, neither exposed to templates. EOL conversion treats a file as binary on a
  NUL byte or a lone CR in the first 8 KB (`lib/src/eol.rs` `is_binary`, "doesn't plan to align the binary
  detection logic with Git", docs/config.md). Diff rendering uses a NUL in the first 8000 bytes and prints
  `(binary)` (`lib/src/diff_presentation/mod.rs`). git-p4son takes binary-ness from Perforce file types
  (`perforce.is_binary_file_type`), so neither matters for it.

## Commit subjects and logs

- `jj log --no-graph --reversed -r '(base..rev) ~ merges()' -T 'commit_id.short() ++ " " ++
  json(description.first_line()) ++ "\n"'` replaces `get_commit_lines_since`. `--reversed` gives oldest first.
- `description.first_line()` is only the first line. git's `%s` joins the first paragraph's lines with spaces: a
  message `base\nsecond line of subject para\n\nbody` gives `base` in jj and `base second line of subject para` in
  git.
- `commit_id.short()` is 12 hex digits; git's `%h` is at least 7. Both resolve in git, so rebase todo lines work.
- `json(self)` for a `Commit` serializes `core/src/backend.rs` `Commit` (parents, change_id, description, author,
  committer...; tree ids skipped), which `jj.py` already uses.
- `find_base_commits` maps onto `jj log -r '::REV & files(root-file:"PATH")'` with
  `self.diff().files()` in the template, but a single walk over many paths needs a union fileset and per-entry
  matching in Python. git's `--stdin` path list has no direct counterpart.

## What only git gives in a colocated repo

- **Batched reads**: `git cat-file --batch` and `--batch-check` read any number of blobs or OIDs in one process.
  jj needs one `jj file show` per file and has no OID output before 0.46.
- **Symlink targets and submodule ids before 0.46**.
- **The index and worktree**: `git status`, `git add`, `git commit`, `git restore`, `git ls-files --others` against
  a temporary index. jj has no index; its working-copy commit is the snapshot. `sync` keeps committing through git.
- **`git merge-file`**: jj has no CLI three-way merge of arbitrary files.
- **`git var GIT_EDITOR`**: jj has its own `ui.editor`.
- **Raw conflicted trees**: git sees a conflicted jj commit as a tree with `.jjconflict-base-*/` and
  `.jjconflict-side-*/` directories; the real conflict is in the `jj:trees` header (docs/git-compatibility.md).
  `git cat-file` at such a commit gives the wrong content, so conflicts must stay rejected (as `jj.py` does) or be
  read through jj.

Every jj commit is a git commit in a colocated repo, so git can read any jj revision by commit id, which is what
`minimal-jj-rebased` does with `change_revision`.

## Stability from jj 0.41 to 0.46

| Feature | Since | Change in 0.41 to 0.46 |
| --- | --- | --- |
| `jj diff --name-only` | 0.19 | none |
| copy and rename info in diffs | 0.21 | 0.44 fix for renames in merge commits |
| `jj diff -T` (`TreeDiffEntry`) | 0.31 | none |
| `TreeDiffEntry.status_char`, `display_diff_path` | 0.37 | none |
| `working-copy.eol-conversion` | 0.32 | none |
| `jj file show -T` | 0.33 | 0.46 adds a full stop to the symlink warning |
| `working-copy.exec-bit-change` | 0.37 | none |
| `TreeEntry.normal_value()`, `TreeValue` | 0.46 | new |
| `json({key => value})` map literals | 0.46 | new |
| `List.get/first/last` return `Option` | 0.46 | breaking for templates that index lists |
| `try(expr, fallback)` | 0.44 | new |
| repeated CLI arguments allowed, last wins | 0.44 | relaxed |
| `RepoPath.absolute()` returns `FsPath` | 0.44 | type change, not used here |

Versions come from the CHANGELOG sections the entries sit in. The live script produced identical output on
0.41.0 and 0.46.0 for `--summary`, `--types`, `--name-only`, the `-T` template, `file show` and `file list`,
apart from `normal_value()` and the warning's full stop. The `Serialize` note in docs/templates.md is the only
written stability promise, and it is a weak one.

Not verified live: rename and copy output on the git backend (the guardrails in this session blocked
`jj git init`, so it comes from jj's test snapshots), Windows separators, and `eol-conversion` on snapshot.
