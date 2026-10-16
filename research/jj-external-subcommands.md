# Can jj run git-p4son as a jj subcommand

Research for wayfinder ticket `06-jj-external-subcommands` (jj-first-class map). Researched 2026-10-16 against jj
source at commit `fd68202c` (main, 2026-10-15) and verified with the jj 0.46.0 release binary
(`jj-v0.46.0-aarch64-apple-darwin`) on macOS. Windows was not tested.

## Answer in short

- jj has **one** mechanism: a user (or repo) config alias that calls `jj util exec`:
  `[aliases] p4son = ["util", "exec", "--", "git-p4son"]`. Then `jj p4son sync` runs `git-p4son sync`.
- jj does **not** look up `jj-<name>` executables on `PATH` the way git does. That was requested (issue #3001) and
  closed by adding `jj util exec` instead (PR #4759), because maintainers preferred explicit aliases.
- `jj util exec` exists since **0.24.0**; it passes the exit code through and sets `JJ_WORKSPACE_ROOT` since
  **0.34.0**. git-p4son's jj support already requires 0.41+, so both are always available.
- `jj util exec` does **not** take the working-copy lock and does **not** snapshot, before or after. The tool runs
  exactly as if launched from the shell, so git-p4son's own lock-and-snapshot logic in `jj.py` is still needed and
  works unchanged.
- The established convention among jj-integrated tools is exactly this: ship a standalone `jj-<tool>` (or
  `<tool>`) executable and document a one-line `jj config set --user aliases.<name> '["util", "exec", "--", ...]'`.

## Mechanisms

### Aliases (`[aliases]`)

An alias maps a name to an argument list for jj itself (docs/config.md, "Aliases"). Plain aliases can only run a
single jj command; running anything else goes through `jj util exec`. Relevant alias history from CHANGELOG.md:

- **0.24.0 (2024-12-04)**: "New command `jj util exec` that can be used for arbitrary aliases."
- **0.34.0 (2025-10-01)**: "`jj util exec` now matches the exit status of the program it runs, and doesn't print
  anything." and "`jj util exec` sets the environment variable `JJ_WORKSPACE_ROOT`".
- **0.42.0 (2026-06-04)**: an alias can be a table with `.definition` and `.doc`; the doc shows in shell completion.
- **0.44.0 (2026-08-05)**: recursion detection relaxed; aliases can expand to bare global flags.
- **0.46.0 (2026-10-07)**: `aliases.<name>.enabled = false` disables an alias (also built-in ones, or ones from
  earlier config layers).

Aliases are read from every config layer, including repo config (`jj config set --repo`, stored under
`.jj/repo/config.toml`), so git-p4son could install the alias per workspace rather than per user. An alias cannot
shadow a built-in command: jj warns "Cannot define an alias that overrides the built-in command 'log'." on every
invocation and runs the built-in (`cli/src/cli_util.rs`, verified). `p4son` does not collide with anything built in.

The documented form (docs/config.md and the `jj util exec --help` text):

```toml
[aliases]
my-script = ["util", "exec", "--", "my-jj-script"]
#                            ^^^^
# This makes sure that flags are passed to your script instead of parsed by jj.
```

Both places carry a warning that the feature "may be removed or replaced by an embedded scripting language in the
future". Nothing in the tracker suggests that is imminent; issue #3575 (extension support, gRPC) is open and
undesigned, and #3034 (jj as a library / RPC API) is a roadmap item.

### `jj util exec` implementation

`cli/src/commands/util/exec.rs` is short. It:

1. Calls `command.workspace_loader()` only to learn the workspace root. It never calls `workspace_helper()`, which is
   what snapshots and locks in other commands.
2. Runs `std::process::Command::new(command).args(args)` with inherited stdin, stdout, stderr, cwd and environment,
   adding `JJ_WORKSPACE_ROOT` when a workspace was found.
3. Calls `std::process::exit(code)` with the child's exit code, or returns an error if the child died by a signal.

There is no PATH magic beyond what Rust's `Command` does (PATH search; on Windows it appends `.exe`). pip installs
`git-p4son` as a console script, so `"git-p4son"` resolves on both platforms (Windows not verified).

### Not available

- `jj-<name>` on `PATH`: issue #3001 "FR: run `jj-*` commands in PATH", closed 2024-11-09 by PR #4759 (`jj util
  exec`). Comments from maintainers (yuja, PhilipMetzger) preferred explicit aliases: they can be inlined, they keep
  `jj<TAB>` uncluttered, and `PATH` lookup was called "a neat hack and a very non-portable way to customize the
  binary".
- Extensions with an API (issue #3575, open) and a stable library/RPC (issue #3034, open): no design yet.

## Behaviour, verified with jj 0.46.0

Test setup: `jj git init --colocate repo`, an unsnapshotted file in `repo/sub/`, and an alias running a Python probe
that prints argv, cwd and `JJ*`/`GIT*` env, and tries `flock(LOCK_EX | LOCK_NB)` on
`.jj/working_copy/working_copy.lock` (the same protocol as `jj_lock.py`).

- **Arguments**: passed verbatim. `jj p4son sync -n --flag 'a b'` gave argv `['sync', '-n', '--flag', 'a b']`.
- **Flags after the alias name**: go to the tool, including jj's own global flags.
  `jj p4son --ignore-working-copy -R foo` gave argv `['--ignore-working-copy', '-R', 'foo']`, and
  `jj p4son --help` reaches the tool too.
- **Without `--` in the alias**: jj parses the tool's flags itself and fails
  (`error: unexpected argument '-I' found`). The `--` is required.
- **jj global flags before the alias**: consumed by jj and not forwarded. `jj --ignore-working-copy --at-op @
  --config ... p4son x` gave the child argv `['x']` and no extra environment, so the child cannot see them (issue
  #9396 reports the same for `--config` and `-R`). `-R` only changes `JJ_WORKSPACE_ROOT`.
- **Working directory**: inherited. Run from `repo/sub`, the child's cwd is `repo/sub`, not the workspace root.
- **`JJ_WORKSPACE_ROOT`**: the workspace root (`.../repo`). With `jj -R ../other p4son` it is `.../other` while the
  cwd stays `repo/sub`. Unset outside a workspace, where the command still runs.
- **Working-copy lock**: free while the child runs; the probe acquired it without blocking.
- **Snapshot**: none. `jj op log --ignore-working-copy` showed no new operation after `jj p4son` even with an
  unsnapshotted file, and a file the child created appeared only at the next `jj st`.
- **Exit code**: passed through (`PROBE_EXIT=3` gave exit 3). A signal gives
  `Error: External command was terminated by signal: 15 (SIGTERM)`.
- **Missing executable**: `Error: Failed to execute external command 'git-p4son-missing'`,
  `Caused by: No such file or directory (os error 2)`, exit 1.
- **stdin**: passed through (piped input reached `cat`).
- **`jj help`**: does not list aliases.
- **Shell completion**: jj's dynamic completion (`COMPLETE=fish` or `COMPLETE=bash jj`) lists the alias name, with
  its `.doc` when defined as a table (`p4doc	Documented alias`). Arguments after the alias complete as file paths
  (`value_hint = FilePath` in `exec.rs`); jj never asks the tool for candidates.

## Implications for git-p4son

- **Lock and snapshot.** Because `jj util exec` neither locks nor snapshots, `jj p4son <cmd>` behaves identically to
  `git p4son <cmd>`: `jj.operation` still snapshots via `jj log -r @`, takes the lock with `jj_lock.py`, and
  reconciles with `jj status` afterwards. No nested-lock situation arises; the `_GIT_P4SON_JJ_LOCK` guard is
  unaffected. If jj ever started holding the lock around `util exec`, git-p4son's non-blocking lock would fail with
  "The jj working copy is busy", so the behaviour is worth a regression test against new jj versions.
- **Workspace discovery.** git-p4son finds the workspace from the cwd (via git). That matches `JJ_WORKSPACE_ROOT`
  except under `jj -R <path> p4son`, where they disagree. Options: honour `JJ_WORKSPACE_ROOT` when set, refuse when
  it differs from the cwd's workspace, or ignore `-R` (today's de facto behaviour). Issue #9396 asks jj itself to
  honour `JJ_WORKSPACE_ROOT` in nested `jj` calls; it is open, and PR #10153 only documents a `-R` workaround.
- **Detecting the jj entry point.** `JJ_WORKSPACE_ROOT` being set is the only signal that git-p4son was launched
  through `jj util exec` (it is also set for any other tool run that way, and by nested calls inheriting the env).
  It could select jj-flavoured help or messages, but should not be the jj detection itself.
- **Global flags.** Users cannot write `jj --ignore-working-copy p4son ...` or `jj --at-op X p4son ...` and expect
  an effect; git-p4son's own flags must go after the alias name.
- **Installation.** `init` (or a `jj` setup step) could offer `jj config set --user aliases.p4son '["util", "exec",
  "--", "git-p4son"]'`, or `--repo` to scope it to the workspace. On jj 0.42+ the table form adds a completion
  description: `aliases.p4son = { definition = ["util", "exec", "--", "git-p4son"], doc = "Perforce bridge" }`.
  The executable name matters: `git-p4son` is the console script name; a `jj-p4son` alias executable is not needed.
- **Completion.** jj completes `jj p4son <TAB>` as file paths only. Matching jj-stack, git-p4son's completion
  scripts (`git_p4son/completions/`) could register for the `jj p4son` prefix too, calling `git-p4son complete`.
  jj's own dynamic completion cannot be extended per alias.
- **Stability caveat.** jj is pre-1.0, `jj util exec` is labelled as possibly replaced by an embedded scripting
  language, and its behaviour changed once already (0.34.0, exit code and env). All of the above is as of 0.46.0.

## Convention among jj-integrated tools

Every tool found ships its own executable and documents a `util exec` alias; none relies on any other mechanism.

- jj-spr (jennings/jj-spr): `jj config set --user aliases.spr '["util", "exec", "--", "jj-spr"]'`, also showing the
  TOML form and a direct binary path variant.
- jj-stack (bos/jj-stack): `stack = ["util", "exec", "--", "jj-stack"]`, plus `eval "$(jj-stack completion zsh
  --jj-alias stack)"` so shell completion covers `jj stack` as well as `jj-stack`.
- jj-vine (abrenneke/jj-vine): `aliases.pr`/`mr`/`vine` to `["util", "exec", "--", "jj-vine"]`; the user picks the
  name.
- jj-pre-push (acarapetis/jj-pre-push, a Python tool on PyPI): `push = ["util", "exec", "--", "uvx", "jj-pre-push",
  "push"]`, or an installed `jj-pre-push push`.

The alias name is the user's choice, the executable keeps its own name, and the tool installs nothing itself; the
README carries the one-liner. jj-stack is the only one found that adapts shell completion to the alias.

## Sources

- jj docs, Aliases: <https://jj-vcs.github.io/jj/latest/config/#aliases> (source `docs/config.md`)
- `jj util exec`: `cli/src/commands/util/exec.rs`; alias resolution: `resolve_aliases` in `cli/src/cli_util.rs`
- CHANGELOG.md entries for 0.24.0, 0.34.0, 0.42.0, 0.44.0, 0.46.0: <https://github.com/jj-vcs/jj/blob/main/CHANGELOG.md>
- Issue #3001 "FR: run `jj-*` commands in PATH" (closed): <https://github.com/jj-vcs/jj/issues/3001>
- PR #4759 "util: add exec command for arbitrary aliases" (merged 2024-11-09): <https://github.com/jj-vcs/jj/pull/4759>
- Issue #9396 "FR: Carry on `--repository` to `jj` commands in `util exec`" (open):
  <https://github.com/jj-vcs/jj/issues/9396>
- Issue #6215 "FR: have `util exec` support arbitrary flags" (open): <https://github.com/jj-vcs/jj/issues/6215>
- Issue #3575 "FR: JJ extension suppport" (open) and #3034 "FR: Using `jj` as library" (open)
- PR #7581 "Support relative paths in jj util exec" (open), PR #10153 "add -R passthru to alias example" (open)
- Tool READMEs: jennings/jj-spr, bos/jj-stack, abrenneke/jj-vine, acarapetis/jj-pre-push on GitHub
- git-p4son: `git_p4son/jj.py`, `git_p4son/jj_lock.py` on branch `minimal-jj-rebased`
