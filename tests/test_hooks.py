"""Tests for git_p4son.hooks module."""

import json
import os
import stat
import sys
import unittest
from unittest import mock

from git_p4son.common import CommandError
from git_p4son.config import ConfigError
from git_p4son.hooks import check_hooks, hook_commands, run_hook
from tests.helpers import WorkspaceTestCase, captured_output, save_config


# Appends its arguments and cwd to hooks.jsonl next to itself, then exits with its code.
_HOOK_SCRIPT = """import json, os, sys
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'hooks.jsonl'), 'a', encoding='utf-8') as f:
    f.write(json.dumps({{'name': {name!r}, 'args': sys.argv[1:], 'cwd': os.getcwd()}}) + '\\n')
sys.exit({exit_code})
"""


class HooksTestCase(WorkspaceTestCase):
    def script(self, name, exit_code=0):
        """A hook script under tools/ that logs its run as name, as a command."""
        path = os.path.join('tools', f'{name}.py')
        self.write_file(path, _HOOK_SCRIPT.format(
            name=name, exit_code=exit_code))
        return [sys.executable, path]

    def runs(self):
        path = os.path.join(self.ws, 'tools', 'hooks.jsonl')
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as f:
            return [json.loads(line) for line in f]

    def configure(self, **hooks):
        save_config(self.ws, {'hooks': hooks})

    def run_hook(self, hook='post-sync'):
        with captured_output() as out:
            failed = run_hook(hook, self.ws, 'jj', 100,
                              '//myclient', synced=101)
        return failed, self.checked_steps(out.getvalue())


class TestHookCommands(HooksTestCase):
    def test_no_hooks(self):
        self.assertEqual(hook_commands('pre-sync', self.ws), [])

    def test_the_commands_in_order(self):
        self.configure(**{'pre-sync': [['a', '$(last-synced)'], ['b']]})
        self.assertEqual(hook_commands('pre-sync', self.ws),
                         [['a', '$(last-synced)'], ['b']])

    def test_invalid_configurations_name_the_setting(self):
        for config, message in (
                ('[hooks]\npre-sync = "a"\n',
                 'hooks.pre-sync in config.toml must be a list of commands'),
                ('[hooks]\npre-sync = ["a", "b"]\n',
                 r'write a single command as \[\["program", "argument", ...\]\]'),
                ('[hooks]\npre-sync = [["a"], "b"]\n', 'command 2 of hooks.pre-sync in config.toml must be a list '
                                                       'of strings'),
                ('[hooks]\npre-sync = [[]]\n',
                 'command 1 of hooks.pre-sync in config.toml is empty'),
                ('[hooks]\npre-sync = [["a", "$(synced)"]]\n', r'command 1 of hooks.pre-sync in config.toml uses '
                 r'\$\(synced\)'),
                ('[hooks]\npre-commit = [["a"]]\n', 'hooks in config.toml has unknown keys: pre-commit. The keys '
                                                    'are: pre-sync, post-sync'),
                ('hooks = 1\n', 'hooks in config.toml must be a table')):
            with self.subTest(config=config):
                self.write_config(config)
                with self.assertRaisesRegex(ConfigError, message):
                    hook_commands('pre-sync', self.ws)

    def test_post_sync_knows_the_synced_changelist(self):
        self.configure(**{'post-sync': [['a', '$(synced)']]})
        self.assertEqual(hook_commands('post-sync', self.ws),
                         [['a', '$(synced)']])


class TestRunHook(HooksTestCase):
    def test_no_hooks_runs_nothing(self):
        failed, out = self.run_hook()
        self.assertEqual((failed, out), (0, ''))

    def test_the_commands_run_in_order_from_the_workspace_root(self):
        first, second = self.script('first'), self.script('second')
        self.configure(
            **{'post-sync': [[*first, '$(last-synced)', '$(depot-root)', '$(backend)'], [*second, '$(synced)']]})
        os.makedirs(os.path.join(self.ws, 'sub'))
        failed, out = self.run_hook()
        self.assertEqual(failed, 0)
        runs = self.runs()
        self.assertEqual([(run['name'], run['args']) for run in runs],
                         [('first', ['100', '//myclient', 'jj']), ('second', ['101'])])
        for run in runs:
            # Windows may give the cwd as a short path.
            self.assertTrue(os.path.samefile(run['cwd'], self.ws))
        self.assertIn('Running post-sync hooks', out)
        self.assertIn('2 hooks ran', out)

    def test_a_failing_command_does_not_stop_the_rest(self):
        self.configure(
            **{'post-sync': [self.script('first', exit_code=3), self.script('second')]})
        failed, out = self.run_hook()
        self.assertEqual(failed, 1)
        self.assertEqual([run['name']
                         for run in self.runs()], ['first', 'second'])
        self.assertIn(
            'Hook command 1 of hooks.post-sync failed with return code 3', out)
        self.assertIn('1 of 2 hooks failed', out)

    def test_a_program_that_cannot_start_counts_as_failed(self):
        self.configure(
            **{'post-sync': [[os.path.join(self.ws, 'no-such-program')], self.script('second')]})
        failed, out = self.run_hook()
        self.assertEqual(failed, 1)
        self.assertEqual([run['name'] for run in self.runs()], ['second'])
        self.assertRegex(
            out, 'Hook command 1 of hooks.post-sync could not start .*no-such-program')


class TestCheckHooks(HooksTestCase):
    def old_hook(self, hook, name, executable=True):
        path = self.write_file(os.path.join(
            '.git-p4son', 'hooks', hook, name), '#!/bin/sh\n')
        if executable:
            os.chmod(path, stat.S_IRWXU)
        return path

    def check_fails(self, message):
        with captured_output() as out, self.assertRaisesRegex(CommandError, message):
            check_hooks(self.ws)
        return self.checked_steps(out.getvalue())

    def test_nothing_to_check(self):
        check_hooks(self.ws)

    def test_invalid_hooks_are_refused(self):
        self.write_config('[hooks]\npre-sync = "a"\n')
        self.check_fails(
            'hooks.pre-sync in config.toml must be a list of commands')

    @mock.patch('git_p4son.hooks_migration._is_windows', return_value=False)
    def test_an_old_hook_file_is_refused_with_the_lines_to_add(self, _windows):
        self.old_hook('pre-sync', 'b-check')
        self.old_hook('pre-sync', 'a-stop')
        self.old_hook('post-sync', 'generate')
        out = self.check_fails(
            'Hooks are configured in \\[hooks\\] in .git-p4son/config.toml')
        self.assertIn('[hooks]\n'
                      'pre-sync = [\n'
                      '  [".git-p4son/hooks/pre-sync/a-stop"],\n'
                      '  [".git-p4son/hooks/pre-sync/b-check"],\n'
                      ']\n'
                      'post-sync = [\n'
                      '  [".git-p4son/hooks/post-sync/generate"],\n'
                      ']\n'
                      'Hooks no longer get GIT_P4SON_BACKEND or GIT_P4SON_REPO_ROOT_DIR: pass $(backend), and the '
                      'working directory is the root.\n', out)

    @mock.patch('git_p4son.hooks_migration._is_windows', return_value=False)
    def test_an_old_hook_file_a_command_names_is_left_alone(self, _windows):
        self.old_hook('pre-sync', 'check')
        self.configure(
            **{'pre-sync': [['.git-p4son/hooks/pre-sync/check', '--fast']]})
        check_hooks(self.ws)

    # Windows takes every file for executable (os.access), so its own never-ran case is notes.txt below.
    @unittest.skipIf(sys.platform == 'win32', 'the executable bit is POSIX')
    @mock.patch('git_p4son.hooks_migration._is_windows', return_value=False)
    def test_a_file_that_never_ran_is_left_alone_with_a_warning(self, _windows):
        self.old_hook('pre-sync', '.DS_Store', executable=False)
        with captured_output() as out:
            check_hooks(self.ws)
        self.assertIn(
            '[warn] Ignoring .git-p4son/hooks/pre-sync/.DS_Store, which never ran as a hook\n',
            self.checked_steps(out.getvalue()))

    @mock.patch('git_p4son.hooks_migration._is_windows', return_value=False)
    def test_the_lines_keep_the_commands_a_hooks_table_has(self, _windows):
        self.old_hook('pre-sync', 'b-check')
        self.configure(**{'pre-sync': [['.git-p4son/hooks/pre-sync/a-check']],
                          'post-sync': [['tools/gen.py']]})
        out = self.check_fails('Hooks are configured in')
        self.assertIn('Set these keys in the [hooks] table of .git-p4son/config.toml:\n'
                      'pre-sync = [\n'
                      '  [".git-p4son/hooks/pre-sync/a-check"],\n'
                      '  [".git-p4son/hooks/pre-sync/b-check"],\n'
                      ']\n', out)
        self.assertNotIn('[hooks]\n', out)
        self.assertNotIn('post-sync', out)

    @mock.patch('git_p4son.hooks_migration._is_windows', return_value=True)
    def test_a_hooks_table_of_only_the_old_runners_gets_the_header(self, _windows):
        self.write_config('[hooks.extension-associations]\npy = "py.exe"\n')
        self.old_hook('pre-sync', 'a.py', executable=False)
        out = self.check_fails('Hooks are configured in')
        self.assertIn('[hooks]\n'
                      'pre-sync = [\n'
                      '  ["py.exe", ".git-p4son/hooks/pre-sync/a.py"],\n'
                      ']\n', out)

    @mock.patch('git_p4son.hooks_migration._is_windows', return_value=True)
    def test_on_windows_the_script_runners_give_the_interpreter(self, _windows):
        self.write_config(
            '[core.script-runners]\n".ps1" = ["pwsh.exe", "-File"]\n')
        self.old_hook('pre-sync', 'stop.ps1', executable=False)
        self.old_hook('pre-sync', 'check.py', executable=False)
        self.old_hook('pre-sync', 'notes.txt', executable=False)
        out = self.check_fails('Hooks are configured in')
        self.assertIn('  ["python.exe", ".git-p4son/hooks/pre-sync/check.py"],\n'
                      '  ["pwsh.exe", "-File", ".git-p4son/hooks/pre-sync/stop.ps1"],\n', out)
        self.assertIn(
            '[warn] Ignoring .git-p4son/hooks/pre-sync/notes.txt, which never ran as a hook\n', out)

    def test_the_script_runners_are_refused_once_the_files_are_named(self):
        for config, setting in (('[core.script-runners]\n".py" = "python.exe"\n', 'core.script-runners'),
                                ('[hooks.extension-associations]\n".py" = "python.exe"\n',
                                 'hooks.extension-associations')):
            with self.subTest(setting=setting):
                self.write_config(config)
                self.check_fails(f'{setting} in config.toml is no longer read')
