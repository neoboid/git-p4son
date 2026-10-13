"""Skill command: installs a Claude Code skill stub and shows the full instructions."""

import argparse
import os
from importlib.resources import files

from . import CONFIG_DIR
from .git import get_workspace_dir
from .log import log

SKILL_NAME = 'git-p4son'
SKILL_OVERRIDE_FILE = 'skill-override.md'


def _skill_text(name: str) -> str:
    """Return a file shipped in the package's skill directory."""
    return (files('git_p4son') / 'skill' / name).read_text(encoding='utf-8')


def claude_config_dir() -> str:
    """Return the Claude Code config directory, honouring CLAUDE_CONFIG_DIR."""
    return (os.environ.get('CLAUDE_CONFIG_DIR')
            or os.path.join(os.path.expanduser('~'), '.claude'))


def is_claude_code_installed() -> bool:
    """Return whether Claude Code's config directory exists."""
    return os.path.isdir(claude_config_dir())


def skill_path() -> str:
    """Return the path the skill stub is installed to."""
    return os.path.join(claude_config_dir(), 'skills', SKILL_NAME, 'SKILL.md')


def is_skill_installed() -> bool:
    """Return whether the skill stub is installed."""
    return os.path.exists(skill_path())


def install_skill() -> str:
    """Write the skill stub, replacing any existing one. Returns its path."""
    path = skill_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(_skill_text('SKILL.md'))
    return path


def skill_install_command(args: argparse.Namespace) -> int:
    """Execute the 'skill install' command."""
    log.heading('Installing Claude Code skill')
    log.success(install_skill())
    return 0


def skill_override_text(workspace_dir: str | None) -> str | None:
    """Return the workspace's own overrides of the instructions, if it has any."""
    if not workspace_dir:
        return None
    path = os.path.join(workspace_dir, CONFIG_DIR, SKILL_OVERRIDE_FILE)
    try:
        with open(path, encoding='utf-8') as f:
            text = f.read()
    except FileNotFoundError:
        return None
    return text if text.strip() else None


def skill_show_command(args: argparse.Namespace) -> int:
    """Execute the 'skill show' command."""
    print(_skill_text('git-p4son.md'), end='')
    override = skill_override_text(get_workspace_dir())
    if override:
        print(f'\n## Overrides for this workspace\n\n'
              f'From `{CONFIG_DIR}/{SKILL_OVERRIDE_FILE}`. Where they '
              f'contradict the instructions above, these win.\n\n'
              f'{override.rstrip()}')
    return 0
