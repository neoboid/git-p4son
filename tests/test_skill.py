"""Tests for git_p4son.skill module."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from git_p4son.cli import main
from git_p4son.skill import (
    install_skill,
    is_skill_installed,
    skill_path,
)


class TestSkillPath(unittest.TestCase):
    def test_honours_claude_config_dir(self):
        with mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': '/cfg'}):
            self.assertEqual(
                skill_path(),
                os.path.join('/cfg', 'skills', 'git-p4son', 'SKILL.md'))

    def test_defaults_to_home(self):
        env = {k: v for k, v in os.environ.items()
               if k != 'CLAUDE_CONFIG_DIR'}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(
                skill_path(),
                os.path.join(os.path.expanduser('~'), '.claude', 'skills',
                             'git-p4son', 'SKILL.md'))


class TestInstallSkill(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.dict(os.environ, {'CLAUDE_CONFIG_DIR': tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_writes_stub(self):
        self.assertFalse(is_skill_installed())
        path = install_skill()
        self.assertTrue(is_skill_installed())
        with open(path, encoding='utf-8') as f:
            stub = f.read()
        self.assertTrue(stub.startswith('---\nname: git-p4son\n'))
        self.assertIn('Run `git p4son skill show`', stub)

    def test_replaces_existing_stub(self):
        path = install_skill()
        with open(path, 'w', encoding='utf-8') as f:
            f.write('old')
        install_skill()
        with open(path, encoding='utf-8') as f:
            self.assertNotEqual(f.read(), 'old')

    def test_install_works_outside_a_workspace(self):
        with mock.patch('sys.argv', ['git-p4son', 'skill', 'install']), \
                mock.patch('git_p4son.cli.get_workspace_dir') as mock_ws, \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(), 0)
        mock_ws.assert_not_called()
        self.assertTrue(is_skill_installed())


class TestSkillShow(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.workspace = tmp.name
        os.makedirs(os.path.join(self.workspace, '.git-p4son'))

    def _show(self, workspace_dir):
        with mock.patch('sys.argv', ['git-p4son', 'skill', 'show']), \
                mock.patch('git_p4son.skill.get_workspace_dir',
                           return_value=workspace_dir), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(main(), 0)
        return out.getvalue()

    def _write_override(self, text):
        path = os.path.join(self.workspace, '.git-p4son', 'skill-override.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def test_prints_instructions(self):
        out = self._show(self.workspace)
        self.assertTrue(out.startswith('# Working with git-p4son\n'))
        self.assertNotIn('Overrides for this workspace', out)

    def test_appends_workspace_overrides(self):
        self._write_override('### Local rule\n\nDo the local thing.\n')
        out = self._show(self.workspace)
        self.assertTrue(out.startswith('# Working with git-p4son\n'))
        self.assertIn('## Overrides for this workspace', out)
        self.assertTrue(out.endswith('Do the local thing.\n'))
        self.assertLess(out.index('## Syncing'), out.index('### Local rule'))

    def test_skips_blank_overrides(self):
        self._write_override('\n  \n')
        self.assertNotIn('Overrides for this workspace',
                         self._show(self.workspace))

    def test_works_outside_a_workspace(self):
        out = self._show(None)
        self.assertTrue(out.startswith('# Working with git-p4son\n'))
        self.assertNotIn('Overrides for this workspace', out)


if __name__ == '__main__':
    unittest.main()
