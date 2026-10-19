"""Tests for git_p4son.init module."""

import os
import tempfile
import tomllib
import unittest
from unittest import mock

from git_p4son import jj
from git_p4son.common import CommandError
from git_p4son.config import load_config
from git_p4son.init import (
    _ask_yes_no,
    _configure_claude_skill,
    _configure_depot_root,
    _configure_jj,
    _configure_split_users,
    _configure_sync_branch,
    _configure_writable_mode,
    init_command,
    _setup_gitignore,
)
from git_p4son.perforce import P4ClientSpec
from git_p4son.sync_split_users import get_split_users, set_split_users
from git_p4son.writable import is_writable_mode, set_writable_mode
from git_p4son.sync_branch import get_sync_branch_setting
from tests.helpers import (
    captured_output, GitRepoTestCase, isolate_jj, make_run_result, requires_jj, run_jj, start_patch, WorkspaceTestCase,
)


class TestSetupGitignore(unittest.TestCase):
    def test_existing_gitignore_left_as_is(self):
        with mock.patch('os.path.exists') as mock_exists:
            mock_exists.side_effect = lambda p: p.endswith('.gitignore')
            result = _setup_gitignore('/ws')
            self.assertEqual(result, '.gitignore already exists')

    def test_copies_p4ignore(self):
        def exists_side_effect(path):
            return path.endswith('.p4ignore')

        with mock.patch('os.path.exists', side_effect=exists_side_effect), \
                mock.patch('shutil.copy2') as mock_copy:
            result = _setup_gitignore('/ws')
            self.assertEqual(result, 'Copied .p4ignore to a new .gitignore')
            mock_copy.assert_called_once_with(
                os.path.join('/ws', '.p4ignore'),
                os.path.join('/ws', '.gitignore'))

    def test_creates_empty_gitignore(self):
        with mock.patch('os.path.exists', return_value=False), \
                mock.patch('builtins.open', mock.mock_open()) as mock_file:
            result = _setup_gitignore('/ws')
            self.assertEqual(result, 'Created an empty .gitignore')
            mock_file.assert_called_once_with(
                os.path.join('/ws', '.gitignore'), 'w')


class TestSelectDepotRoot(unittest.TestCase):
    @mock.patch('builtins.input', side_effect=EOFError)
    def test_eof_aborts_cleanly(self, _input):
        from git_p4son.init import _select_depot_root
        result = _select_depot_root('client', '/ws/sub', '/ws')
        self.assertIsNone(result)

    @mock.patch('git_p4son.depot.p4_depot_root_exists', return_value=True)
    @mock.patch('builtins.input', return_value='')
    def test_entire_workspace_stores_template(self, _input, mock_validate):
        from git_p4son.init import _select_depot_root
        result = _select_depot_root('my-ws', '/ws/sub', '/ws')
        # The stored root keeps the placeholder; validation uses the resolved path.
        self.assertEqual(result, '//$(workspace)')
        mock_validate.assert_called_once_with('//my-ws', '/ws/sub')

    @mock.patch('git_p4son.depot.p4_depot_root_exists', return_value=True)
    @mock.patch('builtins.input', return_value='c')
    def test_current_directory_stores_template(self, _input, mock_validate):
        from git_p4son.init import _select_depot_root
        result = _select_depot_root('my-ws', '/ws/Engine/Source', '/ws')
        self.assertEqual(result, '//$(workspace)/Engine/Source')
        mock_validate.assert_called_once_with(
            '//my-ws/Engine/Source', '/ws/Engine/Source')


class TestConfigureDepotRoot(unittest.TestCase):
    @mock.patch('git_p4son.depot.p4_depot_root_exists', return_value=True)
    @mock.patch('git_p4son.init.get_depot_root', return_value='//depot')
    @mock.patch('git_p4son.init.save_config')
    def test_existing_valid_root_skips_save(self, mock_save, mock_get, mock_validate):
        _configure_depot_root('client', '/ws', '/ws')
        mock_save.assert_not_called()

    @mock.patch('git_p4son.init._select_depot_root', return_value='//depot/new')
    @mock.patch('git_p4son.depot.p4_depot_root_exists', return_value=False)
    @mock.patch('git_p4son.init.get_depot_root', return_value='//depot/old')
    @mock.patch('git_p4son.init.save_config')
    def test_existing_invalid_root_prompts_and_saves(self, mock_save, mock_get,
                                                     mock_validate, mock_select):
        _configure_depot_root('client', '/ws', '/ws')
        mock_save.assert_called_once_with(
            '/ws', {'depot': {'root': '//depot/new'}})

    @mock.patch('git_p4son.init._select_depot_root', return_value='//depot')
    @mock.patch('git_p4son.init.get_depot_root', return_value=None)
    @mock.patch('git_p4son.init.save_config')
    def test_no_existing_root_prompts_and_saves(self, mock_save, mock_get, mock_select):
        _configure_depot_root('client', '/ws', '/ws')
        mock_save.assert_called_once_with(
            '/ws', {'depot': {'root': '//depot'}})

    @mock.patch('git_p4son.init._select_depot_root', return_value=None)
    @mock.patch('git_p4son.init.get_depot_root', return_value=None)
    @mock.patch('git_p4son.init.save_config')
    def test_no_existing_root_user_aborts(self, mock_save, mock_get, mock_select):
        with self.assertRaisesRegex(CommandError, 'No depot root selected'):
            _configure_depot_root('client', '/ws', '/ws')
        mock_save.assert_not_called()

    @mock.patch('git_p4son.depot.p4_depot_root_exists', return_value=True)
    @mock.patch('git_p4son.init.get_depot_root',
                return_value='//$(workspace)/Engine')
    @mock.patch('git_p4son.init.save_config')
    def test_existing_template_root_validated_resolved(self, mock_save,
                                                       mock_get, mock_validate):
        """An existing $(workspace) root is validated against the resolved path."""
        _configure_depot_root('my-ws', '/ws', '/ws')
        mock_validate.assert_called_once_with('//my-ws/Engine', '/ws')
        mock_save.assert_not_called()


_MOCK_SPEC = P4ClientSpec(
    name='my-client', root='/ws',
    options=['noallwrite', 'clobber', 'nocompress'],
    line_end='local')


class TestAskYesNo(unittest.TestCase):
    @mock.patch('builtins.input', return_value='')
    def test_empty_answer_keeps_current_value(self, _input):
        self.assertTrue(_ask_yes_no('Q?', True))
        self.assertFalse(_ask_yes_no('Q?', False))

    @mock.patch('builtins.input', side_effect=['maybe', 'Y'])
    def test_reprompts_until_valid(self, mock_input):
        self.assertTrue(_ask_yes_no('Q?', False))
        self.assertEqual(mock_input.call_count, 2)

    @mock.patch('builtins.input', return_value='no')
    def test_accepts_words(self, _input):
        self.assertFalse(_ask_yes_no('Q?', True))

    @mock.patch('builtins.input', return_value='')
    def test_question_and_default_are_shown_in_the_prompt(self, mock_input):
        _ask_yes_no('Keep it?', True)
        self.assertEqual(mock_input.call_args.args[0],
                         'Keep it? [Y]es / [n]o: ')
        _ask_yes_no('Keep it?', False)
        self.assertEqual(mock_input.call_args.args[0],
                         'Keep it? [y]es / [N]o: ')

    @mock.patch('builtins.input', side_effect=EOFError)
    def test_eof_returns_none(self, _input):
        self.assertIsNone(_ask_yes_no('Q?', False))


class TestConfigureWritableMode(unittest.TestCase):
    @mock.patch('git_p4son.init._ask_yes_no', return_value=True)
    def test_saves_answer(self, _ask):
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(_configure_writable_mode(ws), (True, True))
            self.assertTrue(is_writable_mode(ws))

    @mock.patch('git_p4son.init._ask_yes_no', return_value=True)
    def test_unchanged_answer_is_not_a_change(self, _ask):
        with tempfile.TemporaryDirectory() as ws:
            set_writable_mode(ws, True)
            self.assertEqual(_configure_writable_mode(ws), (True, False))

    @mock.patch('git_p4son.init._ask_yes_no', return_value=False)
    def test_unchanged_answer_leaves_config_untouched(self, _ask):
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(_configure_writable_mode(ws), (False, False))
            self.assertEqual(load_config(ws), {})

    @mock.patch('git_p4son.init._ask_yes_no', return_value=None)
    def test_eof_leaves_config_untouched(self, _ask):
        with tempfile.TemporaryDirectory() as ws:
            self.assertEqual(_configure_writable_mode(ws), (False, False))
            self.assertEqual(load_config(ws), {})


class TestConfigureSplitUsers(WorkspaceTestCase):
    def setUp(self):
        super().setUp()
        self.mock_user = start_patch(self, 'git_p4son.sync_split_users.get_p4_user',
                                     return_value='alice')
        self.mock_log = start_patch(self, 'git_p4son.init.log')

    def _answer(self, answer):
        with mock.patch('git_p4son.init._ask_yes_no',
                        return_value=answer) as mock_ask, \
                mock.patch('builtins.print'):
            _configure_split_users(self.ws)
        return mock_ask

    def test_question_names_the_user_and_defaults_to_off(self):
        mock_ask = self._answer(None)
        mock_ask.assert_called_once_with(
            'Sync your own changelists (alice) individually?', False)

    def test_default_is_on_when_already_configured(self):
        set_split_users(self.ws, ['$(user)'])
        mock_ask = self._answer(None)
        self.assertTrue(mock_ask.call_args.args[1])

    def test_yes_adds_the_placeholder_after_other_users(self):
        set_split_users(self.ws, ['bob'])
        self._answer(True)
        self.assertEqual(get_split_users(self.ws), ['bob', '$(user)'])

    def test_no_removes_only_the_placeholder(self):
        set_split_users(self.ws, ['bob', '$(user)', 'carol'])
        self._answer(False)
        self.assertEqual(get_split_users(self.ws), ['bob', 'carol'])

    def test_unchanged_answer_writes_nothing(self):
        self._answer(False)
        self.assertEqual(load_config(self.ws), {})
        self.mock_log.success.assert_called_once_with('off (unchanged)')

    def test_eof_writes_nothing(self):
        self._answer(None)
        self.assertEqual(load_config(self.ws), {})

    def test_skipped_without_a_current_user(self):
        self.mock_user.return_value = None
        mock_ask = self._answer(True)
        mock_ask.assert_not_called()
        self.mock_log.warning.assert_called_once()
        self.assertEqual(load_config(self.ws), {})


class TestConfigureSyncBranch(GitRepoTestCase):
    def build_workspace(self):
        super().build_workspace()
        self.commit('base')

    def configure(self, answers=(), new_repo=False):
        with mock.patch('builtins.input', side_effect=list(answers) or EOFError), captured_output() as out:
            _configure_sync_branch(self.ws, new_repo)
        return out.getvalue()

    def test_a_new_repo_records_the_branch_head_is_on(self):
        self.git('branch', '-m', 'main', 'trunk')
        out = self.configure(new_repo=True)
        self.assertEqual(get_sync_branch_setting(self.ws), 'trunk')
        self.assertIn('[sync] branch in .git-p4son/config.toml', out)

    def test_an_existing_repo_prefers_main_to_head_s_branch(self):
        self.git('switch', '-q', '-c', 'feature')
        self.configure()
        self.assertEqual(get_sync_branch_setting(self.ws), 'main')

    def test_an_existing_repo_without_main_records_head_s_branch(self):
        self.git('branch', '-m', 'main', 'trunk')
        self.configure()
        self.assertEqual(get_sync_branch_setting(self.ws), 'trunk')

    def test_keeps_an_existing_setting(self):
        self.write_config('[sync]\nbranch = "p4"\n')
        self.configure()
        self.assertEqual(get_sync_branch_setting(self.ws), 'p4')

    def test_detached_head_falls_back_to_main(self):
        self.git('switch', '-q', '--detach')
        self.configure()
        self.assertEqual(get_sync_branch_setting(self.ws), 'main')

    def test_detached_head_without_main_asks(self):
        self.git('branch', 'p4')
        self.git('branch', 'trunk')
        self.git('switch', '-q', '--detach')
        self.git('branch', '-D', 'main')
        out = self.configure(['5', '2'])
        self.assertEqual(get_sync_branch_setting(self.ws), 'trunk')
        self.assertIn('  [1] p4\n  [2] trunk\n', out)

    def test_no_answer_leaves_it_unset(self):
        self.git('branch', 'p4')
        self.git('switch', '-q', '--detach')
        self.git('branch', '-D', 'main')
        out = self.configure()
        self.assertIsNone(get_sync_branch_setting(self.ws))
        self.assertIn(
            'Set [sync] branch in .git-p4son/config.toml before syncing', out)


class TestConfigureClaudeSkill(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config_dir = os.path.join(tmp.name, '.claude')
        patcher = mock.patch.dict(
            os.environ, {'CLAUDE_CONFIG_DIR': self.config_dir})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stub = os.path.join(
            self.config_dir, 'skills', 'git-p4son', 'SKILL.md')

    @mock.patch('builtins.input')
    def test_skipped_without_claude_code(self, mock_input):
        _configure_claude_skill()
        mock_input.assert_not_called()
        self.assertFalse(os.path.exists(self.config_dir))

    @mock.patch('builtins.input', return_value='y')
    def test_installs_when_accepted(self, _input):
        os.makedirs(self.config_dir)
        _configure_claude_skill()
        self.assertTrue(os.path.exists(self.stub))

    @mock.patch('builtins.input', return_value='')
    def test_defaults_to_not_installing(self, _input):
        os.makedirs(self.config_dir)
        _configure_claude_skill()
        self.assertFalse(os.path.exists(self.stub))

    @mock.patch('builtins.input')
    def test_refreshes_installed_skill_without_asking(self, mock_input):
        os.makedirs(os.path.dirname(self.stub))
        with open(self.stub, 'w') as f:
            f.write('old')
        _configure_claude_skill()
        mock_input.assert_not_called()
        with open(self.stub) as f:
            self.assertNotEqual(f.read(), 'old')


class TestConfigureJjWithoutJj(WorkspaceTestCase):
    @mock.patch('builtins.input')
    def test_skipped_without_jj(self, mock_input):
        with mock.patch('git_p4son.jj.run', side_effect=FileNotFoundError('jj')), captured_output():
            self.assertFalse(_configure_jj(self.ws))
        mock_input.assert_not_called()

    @mock.patch('builtins.input')
    def test_skipped_with_jj_older_than_0_41(self, mock_input):
        with mock.patch('git_p4son.jj.run', return_value=make_run_result(stdout=['jj 0.40.0'])), \
                captured_output():
            self.assertFalse(_configure_jj(self.ws))
        mock_input.assert_not_called()


class TestAddP4sonAlias(WorkspaceTestCase):
    """add_p4son_alias, with jj's version scripted and its config commands recorded."""

    def config_sets(self, version):
        """The jj config set commands add_p4son_alias runs with jj at version, as (key, value)."""
        with mock.patch('git_p4son.jj.run', return_value=make_run_result(stdout=[f'jj {version}'])) as mock_run, \
                captured_output():
            jj.add_p4son_alias(self.ws)
        commands = [call.args[0] for call in mock_run.call_args_list]
        return [tuple(command[-2:]) for command in commands if 'set' in command]

    def test_jj_0_42_gets_the_table_form_with_a_doc(self):
        self.assertEqual(self.config_sets('0.42.0'), [
            ('aliases.p4son', '{ definition = ["util", "exec", "--", "git-p4son"], doc = "Run git-p4son" }')])

    def test_jj_0_41_gets_the_plain_form(self):
        self.assertEqual(self.config_sets('0.41.0'), [
            ('aliases.p4son', '["util", "exec", "--", "git-p4son"]')])


@requires_jj
class TestConfigureJj(GitRepoTestCase):
    """_configure_jj in a git repository, with jj installed and a user config of its own."""

    def setUp(self):
        super().setUp()
        isolate_jj(self, self.ws)

    def configure(self, *answers):
        """Run _configure_jj with answers to its questions; returns whether to colocate and the questions asked."""
        with mock.patch('builtins.input', side_effect=list(answers)) as mock_input, captured_output():
            colocate = _configure_jj(self.ws)
        return colocate, [call.args[0] for call in mock_input.call_args_list]

    def alias_definition(self):
        """The alias's command from the user config, in its plain or its table form."""
        with open(os.environ['JJ_CONFIG'], 'rb') as f:
            alias = tomllib.load(f)['aliases']['p4son']
        return alias['definition'] if isinstance(alias, dict) else alias

    def test_an_alias_in_the_table_form_is_found(self):
        run_jj(self.ws, 'git', 'init', '--colocate')
        with open(os.environ['JJ_CONFIG'], 'w') as f:
            f.write(
                '[aliases.p4son]\ndefinition = ["util", "exec", "--", "git-p4son"]\ndoc = "Run git-p4son"\n')
        self.assertEqual(self.configure(), (False, []))

    def test_colocating_also_offers_the_alias(self):
        colocate, questions = self.configure('yes', '')
        self.assertTrue(colocate)
        self.assertEqual(len(questions), 2)
        self.assertIn(
            'Use jj in this workspace too (colocated)?', questions[0])
        self.assertIn('[N]o', questions[0])
        self.assertIn('[Y]es', questions[1])
        self.assertEqual(self.alias_definition(),
                         ['util', 'exec', '--', 'git-p4son'])

    def test_not_colocating_offers_no_alias(self):
        colocate, questions = self.configure('')
        self.assertFalse(colocate)
        self.assertEqual(len(questions), 1)
        self.assertFalse(os.path.exists(os.path.join(self.ws, '.jj')))

    def test_a_jj_workspace_is_only_offered_the_alias(self):
        run_jj(self.ws, 'git', 'init', '--colocate')
        colocate, questions = self.configure('no')
        self.assertFalse(colocate)
        self.assertEqual(len(questions), 1)
        self.assertIn('jj p4son', questions[0])
        self.assertFalse(jj.has_p4son_alias(self.ws))

    def test_rerunning_asks_nothing_once_done(self):
        run_jj(self.ws, 'git', 'init', '--colocate')
        self.configure('yes')
        self.assertEqual(self.configure(), (False, []))

    def test_colocating_matches_git_s_line_endings_from_the_first_snapshot(self):
        self.git('config', 'core.autocrlf', 'true')
        # As git checks the file out with core.autocrlf.
        with open(os.path.join(self.ws, 'a.txt'), 'wb') as f:
            f.write(b'a\r\nb\r\n')
        self.commit('First')
        with captured_output():
            jj.colocate(self.ws)
        self.assertIn('The working copy has no changes',
                      run_jj(self.ws, 'status'))
        self.assertEqual(run_jj(self.ws, 'config', 'get',
                         'working-copy.eol-conversion'), 'input-output')

    def test_colocating_reports_the_files_jj_tracks_that_git_ignores(self):
        self.git('config', 'core.ignorecase', 'true')
        self.write_file('.gitignore', 'ThirdParty/\n')
        self.commit('First')
        self.write_file('Src/Thirdparty/lib.c', 'x\n')
        with captured_output() as out:
            jj.colocate(self.ws)
        self.assertIn('Src/Thirdparty/lib.c', out.getvalue())
        self.assertIn('which git ignores', out.getvalue())

    def test_the_steps_to_colocate_by_hand_match_git_s_line_endings(self):
        self.git('config', 'core.autocrlf', 'input')
        self.assertEqual(jj.colocate_steps(self.ws), [
            'jj --config working-copy.eol-conversion=input git init --colocate',
            'jj config set --repo working-copy.eol-conversion input'])

    def test_colocate_imports_the_branch(self):
        self.commit('First')
        with captured_output():
            jj.colocate(self.ws)
        self.assertTrue(jj.is_workspace(self.ws))
        self.assertIn('main', run_jj(self.ws, 'bookmark', 'list'))


class TestInitCommand(unittest.TestCase):
    def setUp(self):
        self.mock_writable = start_patch(self, 'git_p4son.init._configure_writable_mode',
                                         return_value=(False, False))
        self.mock_split_users = start_patch(
            self, 'git_p4son.init._configure_split_users')
        self.mock_claude_skill = start_patch(
            self, 'git_p4son.init._configure_claude_skill')
        self.mock_sync_branch = start_patch(
            self, 'git_p4son.init._configure_sync_branch')
        self.mock_jj = start_patch(
            self, 'git_p4son.init._configure_jj', return_value=False)
        self.mock_colocate = start_patch(self, 'git_p4son.init.jj.colocate')

    def _make_args(self):
        return mock.Mock(spec=['command', 'verbose'])

    def _next_steps(self, has_commits):
        with mock.patch('git_p4son.init._setup_gitignore',
                        return_value='created'), \
                mock.patch('git_p4son.init.has_commits',
                           return_value=has_commits), \
                mock.patch('git_p4son.git.run'), \
                mock.patch('git_p4son.init.add_files'), \
                mock.patch('git_p4son.init._configure_depot_root',
                           return_value=True), \
                mock.patch('git_p4son.init.get_client_spec',
                           return_value=_MOCK_SPEC), \
                mock.patch('os.path.exists', return_value=True), \
                mock.patch('os.getcwd', return_value='/ws'), \
                captured_output() as out:
            self.assertEqual(init_command(self._make_args()), 0)
        return out.getvalue().partition('Next steps')[2].splitlines()

    def test_configures_split_users(self):
        self._next_steps(has_commits=False)
        self.mock_split_users.assert_called_once_with('/ws')

    def test_configures_the_sync_branch(self):
        self._next_steps(has_commits=False)
        self.mock_sync_branch.assert_called_once_with('/ws', True)

    def test_configures_claude_skill(self):
        self._next_steps(has_commits=False)
        self.mock_claude_skill.assert_called_once_with()

    def test_configures_jj_without_colocating(self):
        self._next_steps(has_commits=False)
        self.mock_jj.assert_called_once_with('/ws')
        self.mock_colocate.assert_not_called()

    def test_an_existing_repo_colocates_at_the_end(self):
        self.mock_jj.return_value = True
        self._next_steps(has_commits=True)
        self.mock_colocate.assert_called_once_with('/ws')

    def test_a_new_repo_colocates_as_a_next_step_after_the_initial_commit(self):
        """jj would snapshot every untracked file before .gitignore is reviewed, so colocating waits for the commit."""
        self.mock_jj.return_value = True
        with mock.patch('git_p4son.init.jj.colocate_steps', return_value=['jj one', 'jj two']):
            steps = self._next_steps(has_commits=False)
        self.mock_colocate.assert_not_called()
        commit = steps.index('* git commit -m "Initial commit"')
        self.assertEqual(steps[commit + 1:commit + 3],
                         ['* jj one', '* jj two'])

    def test_fresh_repo_with_writable_mode_suggests_apply(self):
        self.mock_writable.return_value = (True, True)
        self.assertIn('* git p4son writable apply',
                      self._next_steps(has_commits=False))

    def test_fresh_repo_without_writable_mode_does_not(self):
        steps = self._next_steps(has_commits=False)
        self.assertFalse(any('writable' in step for step in steps))

    def test_existing_repo_suggests_apply_when_mode_changes(self):
        self.mock_writable.return_value = (False, True)
        steps = self._next_steps(has_commits=True)
        self.assertTrue(any('writable apply' in step for step in steps))

    def test_existing_repo_unchanged_mode_suggests_nothing(self):
        self.mock_writable.return_value = (True, False)
        steps = self._next_steps(has_commits=True)
        self.assertFalse(any('writable' in step for step in steps))

    @mock.patch('git_p4son.init._setup_gitignore', return_value='Created an empty .gitignore')
    @mock.patch('git_p4son.init.has_commits', return_value=False)
    @mock.patch('git_p4son.init.add_files')
    @mock.patch('git_p4son.git.run')
    @mock.patch('git_p4son.init._configure_depot_root')
    @mock.patch('git_p4son.init.get_client_spec', return_value=_MOCK_SPEC)
    @mock.patch('os.path.exists', return_value=False)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_success(self, mock_cwd, mock_exists, mock_spec, mock_depot,
                     mock_run, mock_add, mock_has_commits, mock_gitignore):
        result = init_command(self._make_args())
        self.assertEqual(result, 0)
        # git init and git commit; add_files stages .gitignore
        self.assertEqual(mock_run.call_count, 2)
        mock_add.assert_called_once_with(['.gitignore'], '/ws')

    @mock.patch('git_p4son.init.get_client_spec', return_value=None)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_not_p4_workspace(self, mock_cwd, mock_spec):
        with self.assertRaisesRegex(CommandError,
                                    'Not inside a Perforce workspace'):
            init_command(self._make_args())

    @mock.patch('git_p4son.init._setup_gitignore', return_value='.gitignore already exists')
    @mock.patch('git_p4son.init.has_commits', return_value=True)
    @mock.patch('git_p4son.git.run')
    @mock.patch('git_p4son.init._configure_depot_root')
    @mock.patch('git_p4son.init.get_client_spec', return_value=_MOCK_SPEC)
    @mock.patch('os.path.exists', return_value=True)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_existing_repo_skips_git_init(self, mock_cwd, mock_exists,
                                          mock_spec, mock_depot, mock_run,
                                          mock_has_commits, mock_gitignore):
        result = init_command(self._make_args())
        self.assertEqual(result, 0)
        mock_run.assert_not_called()

    @mock.patch('git_p4son.init._setup_gitignore', return_value='.gitignore already exists')
    @mock.patch('git_p4son.init.has_commits', return_value=False)
    @mock.patch('git_p4son.init.add_files')
    @mock.patch('git_p4son.git.run')
    @mock.patch('git_p4son.init._configure_depot_root')
    @mock.patch('git_p4son.init.get_client_spec', return_value=_MOCK_SPEC)
    @mock.patch('os.path.exists', return_value=True)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_existing_repo_without_commits_creates_initial_commit(
            self, mock_cwd, mock_exists, mock_spec, mock_depot, mock_run,
            mock_add, mock_has_commits, mock_gitignore):
        """Re-running init on a repo without commits creates the initial commit."""
        result = init_command(self._make_args())
        self.assertEqual(result, 0)
        mock_add.assert_called_once_with(['.gitignore'], '/ws')
        self.assertEqual([call.args[0] for call in mock_run.call_args_list], [
            ['git', 'commit', '-m', 'Initialize git-p4son repository']])

    @mock.patch('git_p4son.init._configure_depot_root',
                side_effect=CommandError('No depot root selected, aborting'))
    @mock.patch('git_p4son.init.get_client_spec', return_value=_MOCK_SPEC)
    @mock.patch('os.getcwd', return_value='/ws')
    def test_depot_root_abort(self, mock_cwd, mock_spec, mock_depot):
        with self.assertRaisesRegex(CommandError, 'No depot root selected'):
            init_command(self._make_args())


if __name__ == '__main__':
    unittest.main()
