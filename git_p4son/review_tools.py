"""Review tools: Swarm, built in, and the command lines the workspace configures in [review-tool.<name>]."""

import argparse
from typing import Any, TypeGuard

from .backend import alias_for_branch_or_none
from .changelist_store import Review, alias_review, changelist_alias_exists, mark_review_started, \
    set_review_tool
from .common import CommandError, run_interactive
from .config import ConfigError, load_config, set_setting_in_place
from .configured_command import PERFORCE_VARIABLES, check_command_table, command_line
from .log import log

SWARM = 'swarm'

# Seconds to give Swarm to process a shelf before the next step shelves again.
SWARM_SHELF_DELAY = 5

_SECTION = 'review-tool'
_DEFAULT = 'default'
_KEYS = ('command', 'new', 'update')
_BUILT_IN = ('changelist', *PERFORCE_VARIABLES)


def _section(workspace_dir: str) -> dict[str, Any]:
    section = load_config(workspace_dir).get(_SECTION, {})
    if not isinstance(section, dict):
        raise ConfigError(f'{_SECTION} in config.toml is not a table')
    unknown = sorted(key for key, value in section.items()
                     if key != _DEFAULT and not isinstance(value, dict))
    if unknown:
        raise ConfigError(f'[{_SECTION}] in config.toml has unknown keys: {", ".join(unknown)}. '
                          f'The keys are: {_DEFAULT}, and a table per tool')
    for name, reason in ((SWARM, 'swarm is built in'), (_DEFAULT, f'{_DEFAULT} names the default tool')):
        if isinstance(section.get(name), dict):
            raise ConfigError(
                f'[{_SECTION}.{name}] in config.toml can\'t be a review tool, as {reason}. Rename it.')
    return section


def review_tool_names(workspace_dir: str) -> list[str]:
    """The review tools a workspace can use, by name: swarm, then the tables under [review-tool]."""
    return [SWARM, *sorted(name for name, value in _section(workspace_dir).items() if isinstance(value, dict))]


def review_tool_config(tool: str, workspace_dir: str) -> dict[str, list[str]]:
    """The tool's command and its new and update arguments.

    Raises CommandError when the tool is not configured, or its configuration is not valid."""
    config = _section(workspace_dir).get(tool)
    if not isinstance(config, dict):
        raise CommandError(f'Review tool {tool} is not configured. The review tools are: '
                           f'{", ".join(review_tool_names(workspace_dir))}. Configure it again, or record another tool '
                           'with "git p4son review-tool set".')
    return check_command_table(config, _KEYS, _BUILT_IN, f'[{_SECTION}.{tool}]')


def default_review_tool(workspace_dir: str) -> str:
    """The [review-tool] default, swarm without one."""
    default = _section(workspace_dir).get(_DEFAULT, SWARM)
    if not isinstance(default, str):
        raise ConfigError(
            f'{_SECTION}.{_DEFAULT} in config.toml must be a string')
    return default


def runs_command(tool: str | None) -> TypeGuard[str]:
    """Whether git-p4son runs the tool after a shelve: any but Swarm, which takes the shelve itself."""
    return tool is not None and tool != SWARM


def check_review_tool(tool: str, workspace_dir: str, source: str | None = None) -> None:
    """Raise CommandError unless tool is swarm or a configured tool whose configuration is valid.

    source names where the tool was given, for the error."""
    tools = review_tool_names(workspace_dir)
    if tool not in tools:
        given = f' ({source})' if source else ''
        raise CommandError(
            f'Unknown review tool "{tool}"{given}. The review tools are: {", ".join(tools)}')
    if tool != SWARM:
        review_tool_config(tool, workspace_dir)


def resolve_review_tool(name: str | None, workspace_dir: str) -> str:
    """The review tool of a new review: the one named, else [review-tool] default.

    Raises CommandError for a tool that isn't configured or whose configuration isn't valid."""
    tool = name or default_review_tool(workspace_dir)
    check_review_tool(tool, workspace_dir,
                      '--review-tool' if name else f'{_SECTION}.{_DEFAULT} in config.toml')
    return tool


def shelf_delay(tool: str | None) -> int | None:
    """Seconds to sleep between the shelves of a per-commit run: Swarm's delay, none for a tool that is run."""
    return None if runs_command(tool) else SWARM_SHELF_DELAY


def run_review_tool(tool: str, changelist: str, *, first: bool, workspace_dir: str, dry_run: bool) -> None:
    """Run the review tool's command with its new or update arguments from the workspace root, after a shelve.

    The first run creates the review, until one succeeds. Raises CommandError when the tool fails or can't be run."""
    log.heading(f'Running review tool {tool}')
    config = review_tool_config(tool, workspace_dir)
    if dry_run:
        target = f'CL {changelist}' if changelist.isdigit() else 'the new CL'
        log.success(f'Would run review tool {tool} for {target}')
        return
    command = command_line([*config['command'], *config.get('new' if first else 'update', [])],
                           {'changelist': changelist}, workspace_dir)
    try:
        result = run_interactive(command, workspace_dir)
    except OSError as error:
        raise CommandError(f'Review tool {tool} could not start {command[0]}: {error.strerror}. '
                           f'Check command in [{_SECTION}.{tool}] in config.toml.') from error
    if result.returncode != 0:
        raise CommandError(
            f'Review tool {tool} failed for CL {changelist}, with exit code {result.returncode}')
    log.success(f'Ran review tool {tool} for CL {changelist}')


def push_review(alias: str, tool: str, changelist: str, *, first: bool, workspace_dir: str, dry_run: bool) -> None:
    """Run the review tool after a shelve, then record in the alias that the tool has run for the CL."""
    run_review_tool(tool, changelist, first=first,
                    workspace_dir=workspace_dir, dry_run=dry_run)
    if not dry_run:
        mark_review_started(alias, workspace_dir)


def _list(workspace_dir: str) -> None:
    """Print the review tools, marking the built-in one and the default."""
    log.heading('Review tools')
    default = default_review_tool(workspace_dir)
    for tool in review_tool_names(workspace_dir):
        notes = (['built in'] if tool == SWARM else []) + \
            (['default'] if tool == default else [])
        log.info(', '.join([tool, *notes]))


def _describe(review: Review | None) -> str:
    """The tool a review records, and whether the tool has run for it, Swarm's being run by Swarm itself."""
    if review is None:
        return 'None recorded'
    if review.tool == SWARM:
        return SWARM
    return f'{review.tool}, review {"started" if review.started else "not started"}'


def _show(alias: str, workspace_dir: str) -> None:
    log.heading(f'Review tool of alias {alias}')
    log.success(_describe(alias_review(alias, workspace_dir)))


def _current_alias(args: argparse.Namespace) -> str | None:
    """The alias the branch keyword names, if it exists."""
    alias = alias_for_branch_or_none(
        args.backend, args.workspace_dir, new_alias=False)
    return alias if alias is not None and changelist_alias_exists(alias, args.workspace_dir) else None


def review_tool_command(args: argparse.Namespace) -> int:
    """Execute the review-tool command."""
    workspace_dir = args.workspace_dir
    action = args.review_tool_action
    if action == 'set':
        check_review_tool(args.tool, workspace_dir)
        log.heading(f'Setting the review tool of alias {args.alias}')
        set_review_tool(args.alias, args.tool, workspace_dir)
        log.success(_describe(Review(args.alias, args.tool, True)))
    elif action == 'show':
        _show(args.alias, workspace_dir)
    elif action == 'default' and args.tool is not None:
        check_review_tool(args.tool, workspace_dir)
        log.heading('Setting the default review tool')
        set_setting_in_place(workspace_dir, _SECTION, _DEFAULT, args.tool)
        log.success(args.tool)
    elif action == 'default':
        log.heading('Default review tool')
        log.success(default_review_tool(workspace_dir))
    else:
        # Resolved first, so the commands it logs don't land inside the list.
        alias = None if action == 'list' else _current_alias(args)
        _list(workspace_dir)
        if alias is not None:
            _show(alias, workspace_dir)
    return 0
