"""Tests for git_p4son.sync_targets module."""

import os
import sys
from unittest import mock

from git_p4son.common import CommandError
from git_p4son.config import ConfigError
from git_p4son.sync_targets import default_sync_target, resolve_sync_target, sync_target_names
from tests.helpers import FakeP4, WorkspaceTestCase, captured_output, save_config


class TestSyncTargetConfig(WorkspaceTestCase):
    def test_no_targets(self):
        self.assertEqual(sync_target_names(self.ws), [])

    def test_the_configured_targets_by_name(self):
        self.write_sync_target('latest-safe', '1')
        self.write_sync_target('green', '1')
        self.assertEqual(sync_target_names(self.ws), ['green', 'latest-safe'])

    def test_targets_that_are_not_a_table(self):
        self.write_config('[sync]\ntargets = "latest-safe"\n')
        with self.assertRaisesRegex(ConfigError, 'sync.targets in config.toml must be a table'):
            sync_target_names(self.ws)

    def test_invalid_names_are_refused(self):
        for name, message in (
                ('head', 'reserved keyword'),
                ('last-synced', 'reserved keyword'),
                ('branch', 'reserved keyword'),
                ('123', 'all-digit'),
                ('Latest', 'must be lowercase'),
                ('.x', 'Invalid sync target name'),
                ('1_0', 'would be taken as a changelist number'),
                ('-5', 'would be taken as a changelist number')):
            with self.subTest(name=name):
                self.write_config(f'[sync.targets]\n"{name}" = ["x"]\n')
                with self.assertRaisesRegex(ConfigError, message):
                    sync_target_names(self.ws)

    def test_a_table_says_how_to_write_a_target(self):
        for config, setting in (('[sync.targets.latest-safe]\ncommand = ["tools/cl.py"]\n', 'latest-safe'),
                                ('[sync.targets]\nlatest.safe = ["tools/cl.py"]\n', 'latest')):
            with self.subTest(config=config):
                self.write_config(config)
                with self.assertRaisesRegex(ConfigError, f'sync.targets.{setting} in config.toml is a table: write a '
                                            'target as name = \\["program", "argument", ...\\], '
                                            'quoting a name with a dot'):
                    sync_target_names(self.ws)

    def test_a_string_command_is_refused(self):
        self.write_config('[sync.targets]\nlatest-safe = "tools/cl.py"\n')
        with self.assertRaisesRegex(ConfigError, 'sync.targets.latest-safe in config.toml must be a list of '
                                                 'strings, one per argument'):
            sync_target_names(self.ws)

    def test_an_empty_command(self):
        self.write_config('[sync.targets]\nlatest-safe = []\n')
        with self.assertRaisesRegex(ConfigError, 'sync.targets.latest-safe in config.toml is empty'):
            sync_target_names(self.ws)

    def test_an_unknown_variable(self):
        os.environ.pop('NO_SUCH_VARIABLE', None)
        self.write_sync_target('latest-safe', '1', command=[
                               'x', '$(NO_SUCH_VARIABLE)'])
        with self.assertRaisesRegex(ConfigError, r'sync.targets.latest-safe in config.toml uses '
                                                 r'\$\(NO_SUCH_VARIABLE\), which is neither one of '
                                                 'last-synced, depot-root, workspace, user, p4port nor an '
                                                 'environment variable'):
            sync_target_names(self.ws)


class TestDefaultSyncTarget(WorkspaceTestCase):
    def test_none_by_default(self):
        self.assertIsNone(default_sync_target(self.ws))

    def test_the_configured_target_ignoring_case(self):
        self.write_sync_target('latest-safe', '1')
        save_config(self.ws, {'sync': {'default-target': 'Latest-Safe'}})
        self.assertEqual(default_sync_target(self.ws), 'latest-safe')

    def test_a_default_that_is_not_a_target(self):
        self.write_sync_target('latest-safe', '1')
        save_config(self.ws, {'sync': {'default-target': 'green'}})
        with self.assertRaisesRegex(ConfigError, 'sync.default-target in config.toml is "green", which is not a '
                                                 'sync target. The sync targets are: latest-safe'):
            default_sync_target(self.ws)

    def test_a_default_that_is_not_a_string(self):
        self.write_config('[sync]\ndefault-target = 1\n')
        with self.assertRaisesRegex(ConfigError, 'sync.default-target in config.toml must be a string'):
            default_sync_target(self.ws)


class TestResolveSyncTarget(WorkspaceTestCase):
    def setUp(self):
        super().setUp()
        self.p4 = FakeP4(self.ws)
        self.p4.install(self)

    def resolve(self, name='latest-safe', last_synced=100):
        with captured_output() as out:
            changelist = resolve_sync_target(
                name, last_synced, '//myclient', self.ws)
        return changelist, out.getvalue()

    def test_the_changelist_the_command_prints_from_the_workspace_root(self):
        self.write_sync_target('latest-safe', ' 12345\n')
        changelist, out = self.resolve()
        self.assertEqual(changelist, 12345)
        runs = self.sync_target_runs('latest-safe')
        # Windows may give the cwd as a short path.
        self.assertTrue(os.path.samefile(runs[0]['cwd'], self.ws))
        self.assertIn('Resolving sync target latest-safe', out)
        self.assertIn('CL 12345', out)

    def test_the_name_is_matched_ignoring_case(self):
        self.write_sync_target('latest-safe', '12345')
        self.assertEqual(self.resolve('Latest-Safe')[0], 12345)

    def test_the_variables_expand(self):
        self.write_sync_target('latest-safe', '1', command=[
            sys.executable, '.git-p4son/target-latest-safe.py', '--after=$(last-synced)', '$(depot-root)/...',
            '$(workspace)'])
        self.resolve()
        self.assertEqual(self.sync_target_runs('latest-safe')[0]['args'], [
                         '--after=100', '//myclient/...', 'myclient'])

    def test_last_synced_is_empty_before_the_first_sync(self):
        self.write_sync_target('latest-safe', '1', command=[
            sys.executable, '.git-p4son/target-latest-safe.py', '$(last-synced)'])
        self.resolve(last_synced=None)
        self.assertEqual(self.sync_target_runs(
            'latest-safe')[0]['args'], [''])

    def test_a_failing_command(self):
        self.write_sync_target('latest-safe', '', exit_code=3)
        with self.assertRaisesRegex(CommandError, 'Sync target latest-safe failed, with exit code 3'):
            self.resolve()

    def test_output_that_is_not_a_changelist_number_is_shown(self):
        self.write_sync_target('latest-safe', 'CL 12345\nmaybe\n')
        with captured_output() as out, \
                self.assertRaisesRegex(CommandError, 'Sync target latest-safe printed the output above, not a '
                                                     'changelist number'):
            resolve_sync_target('latest-safe', 100, '//myclient', self.ws)
        self.assertIn('CL 12345\nmaybe\n', out.getvalue())

    def test_no_output(self):
        self.write_sync_target('latest-safe', '\n')
        with self.assertRaisesRegex(CommandError, 'Sync target latest-safe printed nothing, not a changelist '
                                                  'number'):
            self.resolve()

    def test_an_unknown_name_lists_the_targets(self):
        self.write_sync_target('latest-safe', '1')
        with self.assertRaisesRegex(CommandError, '"green" is neither a changelist number nor a sync target. '
                                                  'The sync targets are: latest-safe'):
            self.resolve('green')

    def test_an_unknown_name_without_targets(self):
        with self.assertRaisesRegex(CommandError, r'"12a" is neither a changelist number nor a sync target, and '
                                                  r'no sync targets are configured in \[sync.targets\]'):
            self.resolve('12a')

    def test_a_program_that_cannot_start_names_the_setting(self):
        self.write_sync_target('latest-safe', '1', command=[
                               os.path.join(self.ws, 'no-such-program')])
        with self.assertRaisesRegex(CommandError, 'Sync target latest-safe could not start .*no-such-program: .*'
                                                  'Check sync.targets.latest-safe in config.toml'):
            self.resolve()

    def test_a_relative_program_runs_from_the_workspace_root(self):
        self.write_sync_target('latest-safe', '1', command=['tools/cl.exe'])
        with mock.patch('git_p4son.sync_targets.run_for_output') as run:
            run.return_value.returncode = 0
            run.return_value.stdout = ['7']
            self.assertEqual(self.resolve()[0], 7)
        self.assertEqual(run.call_args.args[0], [
                         os.path.join(self.ws, 'tools', 'cl.exe')])
