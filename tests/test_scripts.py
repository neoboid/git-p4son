"""Tests for git_p4son.scripts module."""

import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from git_p4son.config import ConfigError
from git_p4son.scripts import script_command, script_runners
from tests.helpers import WorkspaceTestCase, captured_output


@unittest.skipIf(os.name == 'nt', 'POSIX executable bit test')
class TestPosixScriptCommand(WorkspaceTestCase):
    def test_runs_an_executable_file_itself(self):
        path = Path(self.write_file('run-me', '#!/bin/sh\n'))
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
        self.assertEqual(script_command(
            path, script_runners(self.ws)), [str(path)])

    def test_a_file_that_is_not_executable_has_no_command(self):
        path = Path(self.write_file('skip-me', '#!/bin/sh\n'))
        self.assertIsNone(script_command(
            path, script_runners(self.ws)))


@mock.patch('git_p4son.scripts._is_windows', return_value=True)
class TestWindowsScriptCommand(WorkspaceTestCase):
    def command(self, name: str) -> tuple[Path, list[str] | None]:
        path = Path(self.ws, name)
        return path, script_command(path, script_runners(self.ws))

    def test_default_extensions(self, _is_windows):
        commands = {name: self.command(name)[1] for name in (
            'a.ps1', 'b.sh', 'c.py', 'd.PY')}
        self.assertEqual(commands, {
            'a.ps1': ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
                      str(Path(self.ws, 'a.ps1'))],
            'b.sh': ['bash.exe', str(Path(self.ws, 'b.sh'))],
            'c.py': ['python.exe', str(Path(self.ws, 'c.py'))],
            'd.PY': ['python.exe', str(Path(self.ws, 'd.PY'))],
        })

    def test_core_setting_adds_and_overrides_extensions(self, _is_windows):
        self.write_config('[core.script-runners]\n'
                          '".ps1" = ["pwsh.exe", "-File"]\n'
                          'nu = "nu.exe"\n')
        ps1, ps1_command = self.command('script.ps1')
        nu, nu_command = self.command('script.nu')
        self.assertEqual(ps1_command, ['pwsh.exe', '-File', str(ps1)])
        self.assertEqual(nu_command, ['nu.exe', str(nu)])

    def test_hooks_setting_is_still_read_and_core_wins(self, _is_windows):
        self.write_config('[core.script-runners]\npy = "py.exe"\n'
                          '[hooks.extension-associations]\npy = "python3.exe"\n'
                          '".PY" = "python3.exe"\n'
                          '".nu" = "nu.exe"\n')
        py, py_command = self.command('script.py')
        nu, nu_command = self.command('script.nu')
        self.assertEqual(py_command, ['py.exe', str(py)])
        self.assertEqual(nu_command, ['nu.exe', str(nu)])

    def test_setting_that_is_not_a_table_is_an_error(self, _is_windows):
        for section, key in (('core', 'script-runners'), ('hooks', 'extension-associations')):
            with self.subTest(section=section):
                self.write_config(f'[{section}]\n{key} = "pwsh.exe"\n')
                with self.assertRaises(ConfigError):
                    script_runners(self.ws)

    def test_invalid_core_entry_keeps_the_hooks_one(self, _is_windows):
        self.write_config('[core.script-runners]\n".py" = 3\n'
                          '[hooks.extension-associations]\n".py" = "hooks.exe"\n')
        with captured_output() as out:
            py, command = self.command('script.py')
        self.assertEqual(command, ['hooks.exe', str(py)])
        self.assertEqual(out.getvalue().count('[warn]'), 1)
        self.assertIn('core.script-runners', out.getvalue())

    def test_invalid_entry_is_skipped_with_a_warning(self, _is_windows):
        self.write_config('[core.script-runners]\n".py" = 3\n')
        with captured_output() as out:
            py, command = self.command('script.py')
        self.assertEqual(command, ['python.exe', str(py)])
        self.assertEqual(out.getvalue().count('[warn]'), 1)


if __name__ == '__main__':
    unittest.main()
