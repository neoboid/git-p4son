"""Tests for git_p4son.review_tools module."""

import os
import sys
from unittest import mock

from git_p4son.common import CommandError, RunResult
from git_p4son.config import save_config
from git_p4son.review_tools import SWARM, resolve_review_tool, review_tool_names, run_review_tool
from tests.helpers import FakeP4, WorkspaceTestCase, captured_output


class TestResolveReviewTool(WorkspaceTestCase):
    def test_swarm_is_the_default(self):
        self.assertIsNone(resolve_review_tool(None, self.ws))

    def test_the_configured_default(self):
        self.write_review_tool('my-tool')
        save_config(self.ws, {'review-tool': {'default': 'my-tool'}})
        self.assertEqual(resolve_review_tool(None, self.ws), 'my-tool')

    def test_a_named_tool_over_the_default(self):
        self.write_review_tool('my-tool')
        save_config(self.ws, {'review-tool': {'default': 'my-tool'}})
        self.assertIsNone(resolve_review_tool(SWARM, self.ws))

    def test_the_tools_are_swarm_then_the_configured_ones(self):
        self.write_review_tool('zeta')
        self.write_review_tool('alpha')
        save_config(self.ws, {'review-tool': {'default': 'zeta'}})
        self.assertEqual(review_tool_names(self.ws), [SWARM, 'alpha', 'zeta'])

    def test_an_unknown_tool_lists_the_tools_there_are(self):
        self.write_review_tool('my-tool')
        with self.assertRaisesRegex(CommandError, r'Unknown review tool "other" \(--review-tool\).*swarm, my-tool'):
            resolve_review_tool('other', self.ws)

    def test_an_unknown_default_names_the_setting(self):
        self.write_config('[review-tool]\ndefault = "gone"\n')
        with self.assertRaisesRegex(CommandError, r'Unknown review tool "gone" \(review-tool.default'):
            resolve_review_tool(None, self.ws)

    def test_a_default_that_is_not_a_string(self):
        self.write_config('[review-tool]\ndefault = 1\n')
        with self.assertRaisesRegex(CommandError, 'review-tool.default in config.toml must be a string'):
            resolve_review_tool(None, self.ws)

    def test_a_misspelled_default_is_refused(self):
        self.write_review_tool('my-tool')
        save_config(self.ws, {'review-tool': {'defualt': 'my-tool'}})
        with self.assertRaisesRegex(CommandError, r'\[review-tool\] in config.toml has unknown keys: defualt. '
                                                  'The keys are: default, and a table per tool'):
            resolve_review_tool(None, self.ws)

    def test_a_default_tool_is_refused(self):
        self.write_review_tool('default')
        with self.assertRaisesRegex(CommandError, r'\[review-tool.default\] in config.toml can\'t be a review tool, '
                                                  'as default names the default tool'):
            resolve_review_tool(None, self.ws)

    def test_a_swarm_tool_is_refused(self):
        self.write_review_tool('swarm')
        with self.assertRaisesRegex(CommandError, 'swarm is built in'):
            resolve_review_tool(None, self.ws)


class TestReviewToolConfig(WorkspaceTestCase):
    def test_a_string_command_is_refused(self):
        self.write_config(
            '[review-tool.my-tool]\ncommand = "python tool.py"\n')
        with self.assertRaisesRegex(CommandError, r'command in \[review-tool.my-tool\] in config.toml must be a '
                                                  'list of strings, one per argument'):
            resolve_review_tool('my-tool', self.ws)

    def test_a_string_suffix_is_refused(self):
        self.write_review_tool('my-tool', update='update')
        with self.assertRaisesRegex(CommandError, 'update in .* must be a list of strings'):
            resolve_review_tool('my-tool', self.ws)

    def test_a_tool_without_a_command(self):
        self.write_config('[review-tool.my-tool]\nnew = ["create"]\n')
        with self.assertRaisesRegex(CommandError, r'\[review-tool.my-tool\] in config.toml has no command'):
            resolve_review_tool('my-tool', self.ws)

    def test_an_empty_command(self):
        self.write_review_tool('my-tool', command=[])
        with self.assertRaisesRegex(CommandError, 'command in .* is empty'):
            resolve_review_tool('my-tool', self.ws)

    def test_an_unknown_key(self):
        self.write_review_tool('my-tool', delete=['delete'])
        with self.assertRaisesRegex(CommandError, 'unknown keys: delete. The keys are: command, new, update'):
            resolve_review_tool('my-tool', self.ws)

    @mock.patch.dict(os.environ, clear=False)
    def test_an_unknown_variable_is_refused_before_anything_runs(self):
        os.environ.pop('NO_SUCH_VARIABLE', None)
        self.write_review_tool(
            'my-tool', new=['create', '$(NO_SUCH_VARIABLE)'])
        with self.assertRaisesRegex(CommandError, r'uses \$\(NO_SUCH_VARIABLE\), which is neither one of '
                                                  'changelist, workspace, user, p4port nor an environment variable'):
            resolve_review_tool('my-tool', self.ws)


class TestRunReviewTool(WorkspaceTestCase):
    def setUp(self):
        super().setUp()
        self.p4 = FakeP4(self.ws)
        self.p4.install(self)

    def run_tool(self, first=True, dry_run=False, tool='my-tool'):
        with captured_output() as out:
            run_review_tool(tool, '101', first=first,
                            workspace_dir=self.ws, dry_run=dry_run)
        return out.getvalue()

    def test_a_first_run_creates_the_review_from_the_workspace_root(self):
        self.write_review_tool('my-tool')
        out = self.run_tool()
        runs = self.review_tool_runs('my-tool')
        # Windows may give the cwd as a short path.
        self.assertTrue(os.path.samefile(runs[0].pop('cwd'), self.ws))
        self.assertEqual(runs, [{'args': ['create', '101']}])
        self.assertIn(' create 101\n', out)
        self.assertIn('Ran review tool my-tool for CL 101', out)

    def test_a_later_run_updates_it(self):
        self.write_review_tool('my-tool')
        self.run_tool(first=False)
        self.assertEqual(self.review_tool_runs('my-tool')
                         [0]['args'], ['update', '101'])

    def test_a_missing_suffix_runs_the_command_as_it_is(self):
        self.write_review_tool('my-tool', update=None)
        self.run_tool(first=False)
        self.assertEqual(self.review_tool_runs('my-tool')[0]['args'], [])

    @mock.patch.dict(os.environ, {'MY_REVIEW_TOKEN': 'secret'})
    def test_the_variables_expand_within_an_argument(self):
        self.write_review_tool('my-tool', new=[
            '--client=$(workspace)', '$(user)@$(p4port)', '$(MY_REVIEW_TOKEN)', '$(changelist)-$(changelist)'])
        self.run_tool()
        self.assertEqual(self.review_tool_runs('my-tool')[0]['args'], [
            '--client=myclient', 'me@ssl:perforce.example.com:1666', 'secret', '101-101'])

    def test_an_expanded_value_stays_one_argument(self):
        self.p4.user = 'some one'
        self.write_review_tool('my-tool', new=['$(user)'])
        self.run_tool()
        self.assertEqual(self.review_tool_runs(
            'my-tool')[0]['args'], ['some one'])

    @mock.patch.dict(os.environ, {'user': 'from-the-environment', 'USER': 'from-the-environment'})
    def test_a_built_in_variable_wins_over_the_environment(self):
        self.write_review_tool('my-tool', new=['$(user)'])
        self.run_tool()
        self.assertEqual(self.review_tool_runs('my-tool')[0]['args'], ['me'])

    def test_perforce_is_only_asked_for_the_variables_used(self):
        self.write_review_tool('my-tool')
        self.run_tool()
        self.assertEqual(self.p4.calls, [])

    def test_p4s_default_port_when_p4port_is_not_set(self):
        self.p4.port = ''
        self.write_review_tool('my-tool', new=['$(p4port)'])
        self.run_tool()
        self.assertEqual(self.review_tool_runs('my-tool')
                         [0]['args'], ['perforce:1666'])

    def test_a_failing_tool_raises(self):
        self.write_review_tool('my-tool', fail_at=1)
        with self.assertRaisesRegex(CommandError, 'Review tool my-tool failed for CL 101, with exit code 3'):
            self.run_tool()

    def test_a_dry_run_runs_nothing(self):
        self.write_review_tool('my-tool')
        out = self.run_tool(dry_run=True)
        self.assertEqual(self.review_tool_runs('my-tool'), [])
        self.assertIn('Would run review tool my-tool for CL 101', out)

    def test_a_tool_no_longer_configured(self):
        self.write_review_tool('other')
        with self.assertRaisesRegex(CommandError, 'Review tool my-tool is not configured. The review tools are: '
                                                  'swarm, other'):
            self.run_tool()

    def test_a_program_that_cannot_start_names_the_setting(self):
        self.write_review_tool(
            'my-tool', command=[os.path.join(self.ws, 'no-such-program')])
        with self.assertRaisesRegex(CommandError, 'Review tool my-tool could not start .*no-such-program: .*'
                                                  r'Check command in \[review-tool.my-tool\] in config.toml'):
            self.run_tool()

    def test_a_relative_program_runs_from_the_workspace_root_wherever_git_p4son_runs(self):
        self.write_review_tool(
            'my-tool', command=['tools/review.exe', '$(changelist)'])
        with mock.patch('git_p4son.review_tools.run_interactive', return_value=RunResult(0, [], [])) as run:
            self.run_tool()
        self.assertEqual(run.call_args.args[0], [os.path.join(
            self.ws, 'tools', 'review.exe'), '101', 'create', '101'])

    def test_a_program_on_path_is_left_to_the_path_search(self):
        self.write_review_tool('my-tool', command=['review', '$(changelist)'])
        with mock.patch('git_p4son.review_tools.run_interactive', return_value=RunResult(0, [], [])) as run:
            self.run_tool()
        self.assertEqual(run.call_args.args[0], [
                         'review', '101', 'create', '101'])

    def test_the_program_is_found_on_path(self):
        self.write_review_tool(
            'my-tool', command=[os.path.basename(sys.executable), '.git-p4son/my-tool.py'])
        with mock.patch.dict(os.environ, {'PATH': os.path.dirname(sys.executable)}):
            self.run_tool()
        self.assertEqual(self.review_tool_runs('my-tool')
                         [0]['args'], ['create', '101'])
