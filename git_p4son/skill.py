"""
Skill command implementation for git-p4son.

Installs a Claude Code skill that teaches AI agents how to use git-p4son. The
installed SKILL.md is only a stub telling the agent to run `git p4son skill
show`, which prints the full instructions from the installed package, so the
skill stays current across git-p4son upgrades without being reinstalled.
"""

import argparse
import os
from importlib.resources import files

from .log import log

SKILL_NAME = 'git-p4son'


def _skill_text(name: str) -> str:
    """Return a file shipped in the package's skill directory."""
    return (files('git_p4son') / 'skill' / name).read_text(encoding='utf-8')


def claude_config_dir() -> str:
    """Return the Claude Code config directory, honouring CLAUDE_CONFIG_DIR."""
    return (os.environ.get('CLAUDE_CONFIG_DIR')
            or os.path.join(os.path.expanduser('~'), '.claude'))


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


def skill_show_command(args: argparse.Namespace) -> int:
    """Execute the 'skill show' command."""
    print(_skill_text('git-p4son.md'), end='')
    return 0


def skill_command(args: argparse.Namespace) -> int:
    """Dispatch skill subcommands."""
    if args.skill_action == 'install':
        return skill_install_command(args)
    elif args.skill_action == 'show':
        return skill_show_command(args)
    else:
        log.error(
            'No skill action specified. Use "git-p4son skill -h" for help.')
        return 1
