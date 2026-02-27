# Default Message Analysis

## Problem

The `-m`/`--message` flag is `required=True` for both `new` and `review` commands. This is inconvenient when the HEAD
commit subject is a good default.

## Solution

Make `-m` optional with `default=None`. When omitted, `run_command` resolves the message from the HEAD commit subject
via `get_head_subject()`. If no commits exist, it returns an error.

## Design Decisions

- **`get_head_subject` in `common.py`**: Follows the same pattern as `get_current_branch` — uses raw `subprocess.run`,
  returns `str | None`, catches exceptions.
- **Resolution in `run_command`**: Both `new` and `review` share the same resolution block, placed after branch
  resolution and before command dispatch.
- **Explicit `-m` takes precedence**: When provided, `args.message` is non-None so the resolution block is skipped
  entirely.
