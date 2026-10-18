"""Tests for git_p4son.jj and git_p4son.jj_lock modules."""

import dataclasses
import os
import stat
import subprocess
import unittest
from unittest import mock

from git_p4son import jj
from git_p4son.common import CommandError, RunError, run
from git_p4son.perforce import P4SyncAbortError
from git_p4son.backend import completion_backend, open_backend
from git_p4son.git_backend import GitBackend
from git_p4son.jj_backend import JjBackend
from git_p4son.jj_lock import working_copy_lock
from git_p4son.changelist_store import alias_changes, load_changelist_alias
from git_p4son.cli import create_parser, run_command
from git_p4son.log import log
from tests.helpers import (
    captured_output, isolate_jj, JjTestCase, make_run_result, P4RepoTestCase, requires_jj, run_jj, start_patch,
    WorkspaceTestCase,
)


class TestCheckInvokedInsideWorkspace(WorkspaceTestCase):
    """jj util exec, behind the jj p4son alias, sets JJ_WORKSPACE_ROOT to the workspace jj -R names."""

    def check(self, cwd, root):
        """Run the check from cwd with JJ_WORKSPACE_ROOT set to root, or unset; returns it as the check left it."""
        with mock.patch.dict(os.environ):
            os.environ.pop('JJ_WORKSPACE_ROOT', None)
            if root is not None:
                os.environ['JJ_WORKSPACE_ROOT'] = root
            jj.check_invoked_inside_workspace(cwd)
            return os.environ.get('JJ_WORKSPACE_ROOT')

    def test_unset_is_fine(self):
        self.check(self.ws, None)

    def test_inside_the_workspace_is_fine_and_hooks_do_not_inherit_it(self):
        sub = os.path.join(self.ws, 'sub')
        os.makedirs(sub)
        self.assertIsNone(self.check(self.ws, self.ws))
        self.assertIsNone(self.check(sub, self.ws))

    def test_another_workspace_is_refused(self):
        other = os.path.join(self.ws, 'other')
        os.makedirs(other)
        with self.assertRaisesRegex(CommandError, 'Run jj p4son from inside the workspace; -R is not supported'):
            self.check(self.ws, other)


class TestWorkingCopyLock(WorkspaceTestCase):
    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.ws, '.jj', 'working_copy'))

    def test_busy_lock_raises(self):
        with working_copy_lock(self.ws):
            with self.assertRaisesRegex(CommandError, 'busy'):
                with working_copy_lock(self.ws):
                    pass

    def test_lock_is_released(self):
        with working_copy_lock(self.ws):
            pass
        with working_copy_lock(self.ws):
            pass


class TestOperationOutsideJj(WorkspaceTestCase):
    def test_yields_none_outside_a_jj_workspace(self):
        with jj.operation(self.ws) as workspace:
            self.assertIsNone(workspace)


@requires_jj
class JjRepoTestCase(WorkspaceTestCase):
    """A colocated jj repo: main at "base", feat at "feature" on top, and an empty working change above feat."""

    def build_workspace(self):
        isolate_jj(self, self.ws)
        for command in (['git', 'init', '--colocate'],
                        ['describe', '-m', 'base'], ['bookmark',
                                                     'create', 'main', '-r', '@'],
                        ['new'], ['describe', '-m', 'feature'],
                        ['bookmark', 'create', 'feat', '-r', '@'], ['new']):
            if command == ['describe', '-m', 'base']:
                self.write_file('a.txt', 'a\n')
            if command == ['describe', '-m', 'feature']:
                self.write_file('b.txt', 'b\n')
            self.jj(*command)

    def setUp(self):
        super().setUp()
        isolate_jj(self, self.ws)

    def jj(self, *args):
        return run_jj(self.ws, *args)

    def commit_of(self, revision):
        # Without --ignore-working-copy, jj would wait for the lock a test may hold.
        return self.jj('--ignore-working-copy', 'log', '-r', revision, '--no-graph', '-T', 'commit_id')

    def operation(self, **kwargs):
        """jj.operation with its output captured."""
        out = captured_output()
        out.__enter__()
        self.addCleanup(out.__exit__, None, None, None)
        return jj.operation(self.ws, **kwargs)


class TestJjBackend(JjRepoTestCase):
    """JjBackend over the workspace, whose tip is the feature change with bookmark feat on main."""

    def test_a_bookmark_names_itself_and_its_commit(self):
        with self.operation() as workspace:
            backend = JjBackend(workspace)
            self.assertEqual(backend.base_name('main'), 'main')
            self.assertEqual(backend.base_name(self.commit_of('main')), 'main')
            with self.assertRaisesRegex(CommandError, '@ names no bookmark'):
                backend.base_name('@')

    def test_a_commit_s_identity_is_its_change_id(self):
        with self.operation() as workspace:
            commits = [self.commit_of('feat'), self.commit_of('main')]
            self.assertEqual(JjBackend(workspace).commit_identities(commits), [
                self.jj('--ignore-working-copy', 'log', '-r',
                        name, '--no-graph', '-T', 'change_id')
                for name in ('feat', 'main')])
            self.assertEqual(JjBackend(workspace).commit_identities([]), [])

    def test_answers_from_the_tip(self):
        with self.operation() as workspace:
            backend = JjBackend(workspace)
            self.assertEqual(backend.resolve_base(
                None), self.commit_of('main'))
            self.assertEqual(backend.suggested_alias(), 'feat')
            self.assertEqual(backend.subject(), 'feature')
            self.assertEqual(sorted(backend.base_candidates()),
                             [('feat', 'jj bookmark'), ('main', 'jj bookmark')])
            self.assertEqual(
                (backend.words.tool, backend.words.branch), ('jj', 'bookmark'))

    def test_explicit_head_parent_base_is_the_tip_parent(self):
        """-b HEAD~1, the documented default, means the tip's parent here too, as it did before -b defaulted to None."""
        with self.operation() as workspace:
            self.assertEqual(JjBackend(workspace).resolve_base(
                'HEAD~1'), self.commit_of('main'))

    def test_no_subject_without_a_description(self):
        self.jj('describe', '-m', '', '-r', 'feat')
        with self.operation() as workspace, self.assertRaisesRegex(CommandError, 'no description'):
            JjBackend(workspace).subject()

    def test_chosen_for_a_jj_workspace(self):
        with open_backend(self.ws, None) as backend:
            self.assertIsInstance(backend, JjBackend)
        self.assertIsInstance(completion_backend(self.ws, None), JjBackend)
        self.assertIsInstance(completion_backend(self.ws, 'git'), GitBackend)

    def test_setting_chooses_git(self):
        self.write_config('[core]\nbackend = "git"\n')
        self.assertIsInstance(completion_backend(self.ws, None), GitBackend)
        self.assertIsInstance(completion_backend(self.ws, 'jj'), JjBackend)

    def test_git_backend_still_holds_the_lock(self):
        with open_backend(self.ws, 'git') as backend:
            self.assertIsInstance(backend, GitBackend)
            with self.assertRaisesRegex(CommandError, 'busy'):
                with working_copy_lock(self.ws):
                    pass

    def test_git_backend_needs_no_jj(self):
        with mock.patch('git_p4son.jj.run', side_effect=FileNotFoundError('jj')), \
                open_backend(self.ws, 'git') as backend:
            self.assertIsInstance(backend, GitBackend)

    def test_missing_jj_points_to_the_git_backend(self):
        with mock.patch('git_p4son.jj.run', side_effect=FileNotFoundError('jj')), \
                self.assertRaisesRegex(CommandError, 'jj could not be run.*--backend git'):
            with open_backend(self.ws, None):
                pass

    def test_old_jj_points_to_the_git_backend(self):
        with mock.patch('git_p4son.jj.run', return_value=make_run_result(stdout=['jj 0.40.0'])), \
                self.assertRaisesRegex(CommandError, '0.41 or newer.*--backend git'):
            with open_backend(self.ws, None):
                pass


class TestLineEndingCheck(JjRepoTestCase):
    def git(self, *args):
        subprocess.run(['git', *args], cwd=self.ws, check=True)

    def test_mismatch_is_reported_with_the_fix(self):
        self.git('config', 'core.autocrlf', 'true')
        with captured_output() as out, jj.operation(self.ws):
            pass
        self.assertIn(
            "jj's working-copy.eol-conversion is none, but git's core.autocrlf is true", out.getvalue())
        self.assertIn(
            'jj config set --repo working-copy.eol-conversion input-output', out.getvalue())

    def test_matching_settings_are_quiet(self):
        with self.operation() as workspace:
            for autocrlf, eol in (('false', 'none'), ('true', 'input-output'), ('input', 'input')):
                with self.subTest(autocrlf=autocrlf), captured_output() as out:
                    self.git('config', 'core.autocrlf', autocrlf)
                    dataclasses.replace(
                        workspace, eol_conversion=eol).check_line_endings()
                self.assertNotIn(
                    'write line endings differently', out.getvalue())

    def test_the_git_backend_is_not_warned(self):
        self.git('config', 'core.autocrlf', 'true')
        with captured_output() as out, jj.operation(self.ws, lock_only=True):
            pass
        self.assertNotIn('write line endings differently', out.getvalue())


class TestWorkspace(JjRepoTestCase):
    def test_tip_is_the_parent_of_an_empty_new_change(self):
        with self.operation() as workspace:
            self.assertEqual(workspace.tip.commit, self.commit_of('feat'))
            self.assertEqual(workspace.tip.description.strip(), 'feature')
            self.assertEqual(workspace.only_bookmark(), 'feat')
            self.assertEqual(JjBackend(workspace).revision,
                             self.commit_of('feat'))

    def test_tip_is_the_working_change_once_it_has_a_description(self):
        self.jj('describe', '-m', 'more')
        with self.operation() as workspace:
            self.assertEqual(workspace.tip.commit, self.commit_of('@'))
            self.assertIsNone(workspace.only_bookmark())

    def test_default_base_is_the_tip_parent(self):
        with self.operation() as workspace:
            self.assertEqual(workspace.resolve_base(
                None), self.commit_of('main'))

    def test_named_base_is_resolved_to_its_commit(self):
        with self.operation() as workspace:
            self.assertEqual(workspace.resolve_base(
                'main'), self.commit_of('main'))

    def test_snapshot_check_passes_when_jj_tracks_everything(self):
        with self.operation() as workspace:
            workspace.check_snapshot()

    def test_nested_operation_needs_the_git_backend(self):
        with self.operation():
            with self.assertRaisesRegex(CommandError, '--backend git'):
                with jj.operation(self.ws):
                    pass
            with open_backend(self.ws, 'git') as nested:
                self.assertIsInstance(nested, GitBackend)

    def test_busy_working_copy_fails_instead_of_waiting(self):
        with working_copy_lock(self.ws):
            with self.assertRaisesRegex(CommandError, 'busy'):
                with self.operation():
                    pass

    def test_git_operation_in_progress_is_refused(self):
        open(os.path.join(self.ws, '.git', 'MERGE_HEAD'), 'w').close()
        with self.assertRaisesRegex(CommandError, 'git operation is in progress'):
            with self.operation():
                pass


@requires_jj
class JjP4RepoTestCase(JjTestCase, P4RepoTestCase):
    """git-p4son commands in a colocated jj repo on top of the synced workspace, whose main jj tracks as a bookmark."""

    def build_workspace(self):
        super().build_workspace()
        self.write_config('[depot]\nroot = "//myclient"\n')
        self.commit('Add config')

    def describe(self, revision, template='description.first_line()'):
        return self.jj('--ignore-working-copy', 'log', '-r', revision, '--no-graph', '-T', template)

    def p4son(self, *argv):
        """Run git-p4son with argv as the command line would; returns everything it printed.

        If the command fails, what it printed is added to the failure."""
        try:
            with captured_output() as out, \
                    mock.patch('git_p4son.cli.get_workspace_dir', return_value=self.ws):
                self.assertEqual(run_command(
                    create_parser().parse_args(argv)), 0)
        except Exception as error:
            error.add_note(
                f'git p4son {" ".join(argv)} printed:\n{out.getvalue()}')
            raise
        return out.getvalue()

    def p4son_fails(self, *argv, message):
        """Run git-p4son with argv, expecting a CommandError matching message; returns everything it printed."""
        with captured_output() as out, \
                mock.patch('git_p4son.cli.get_workspace_dir', return_value=self.ws), \
                self.assertRaisesRegex(CommandError, message):
            run_command(create_parser().parse_args(argv))
        return out.getvalue()

    def working(self):
        """The working change's commit, after jj snapshots the files on disk."""
        self.jj('status')
        return self.describe('@', 'commit_id')


class TestJjNew(JjP4RepoTestCase):
    def build_workspace(self):
        super().build_workspace()
        self.jj('new', 'main', '-m', 'Change a')
        self.edit('a.txt', 'a2\n')
        self.jj('bookmark', 'create', 'feat', '-r', '@')
        self.jj('new')

    def test_new_takes_the_change_its_description_and_bookmark_from_jj(self):
        self.p4son('new', '-b', 'main')
        self.assertEqual(self.p4.pending,
                         {'101': ['Change a', '', 'Changes included:', '1. Change a']})
        self.assertEqual(self.p4.opened, {'a.txt': ('101', 'edit')})
        self.assertEqual(load_changelist_alias('feat', self.ws), '101')
        self.assertEqual(self.read('a.txt'), 'a2\n')

    def feat_change(self):
        return self.describe('feat', 'change_id')

    def test_new_records_the_change_which_branch_finds_after_rewrites(self):
        self.p4son('new', '-b', 'main')
        self.assertEqual(alias_changes(self.ws), {'feat': self.feat_change()})
        # A new description, a bookmark rename and a second bookmark all leave the change ID as it was.
        self.jj('describe', '-r', 'feat', '-m', 'Change a, reworded')
        self.jj('bookmark', 'rename', 'feat', 'feature-a')
        self.jj('bookmark', 'create', 'extra', '-r', 'feature-a')
        self.assertEqual(self.p4son('alias', 'show'), '101\n')
        out = self.p4son('update', '--no-edit')
        self.assertIn('branch -> feat', out)

    def test_a_change_without_a_bookmark_is_named_by_its_change_id(self):
        self.jj('new', 'main', '-m', 'Change b')
        self.edit('b.txt', 'b2\n')
        change = self.describe('@', 'change_id')
        self.p4son('new', '-b', 'main')
        self.assertEqual(load_changelist_alias(change[:8], self.ws), '101')
        self.assertEqual(alias_changes(self.ws), {change[:8]: change})

    def test_one_alias_per_change(self):
        self.p4son('new', '-b', 'main')
        self.p4son_fails('new', 'other', '--no-edit', '-b', 'main',
                         message='Alias "feat" already records this jj change, for CL 101')
        self.p4son('new', 'other', '--no-edit', '--force', '-b', 'main')
        self.assertEqual(alias_changes(self.ws), {'other': self.feat_change()})
        self.assertEqual(load_changelist_alias('feat', self.ws), '101')

    def test_alias_new_records_no_change(self):
        self.p4son('alias', 'new', '555', 'named')
        self.assertEqual(alias_changes(self.ws), {})

    def test_alias_list_shows_the_change_and_marks_it_gone(self):
        self.p4son('new', '-b', 'main')
        short = self.feat_change()[:8]
        self.assertIn(
            f'feat -> CL 101, jj change {short}\n', self.p4son('alias', 'list'))
        self.jj('abandon', 'feat')
        self.assertIn(
            f'feat -> CL 101, jj change {short} (gone)\n', self.p4son('alias', 'list'))

    def test_alias_list_ignores_a_change_that_is_no_change_id(self):
        self.p4son('new', '-b', 'main')
        self.write_file('.git-p4son/aliases/bad.toml',
                        'changelist = 102\nchange = "foo bar"\n')
        listing = self.p4son('alias', 'list')
        self.assertIn('bad -> CL 102\n', listing)
        self.assertIn(
            f'feat -> CL 101, jj change {self.feat_change()[:8]}\n', listing)

    def test_new_takes_a_described_working_change_git_has_not_committed(self):
        self.jj('describe', '-m', 'Change b')
        self.edit('b.txt', 'b2\n')
        self.jj('bookmark', 'set', 'feat', '-r', '@', '--allow-backwards')
        self.p4son('new', '-b', 'main')
        self.assertEqual(self.p4.pending['101'],
                         ['Change b', '', 'Changes included:', '1. Change a', '2. Change b'])
        self.assertEqual(self.p4.opened, {'a.txt': (
            '101', 'edit'), 'b.txt': ('101', 'edit')})
        self.assertEqual(self.read('b.txt'), 'b2\n')

    def describe_change_b(self):
        """Describe the working change, which git has not committed, as Change b adding d.txt."""
        self.jj('describe', '-m', 'Change b')
        self.write_file('d.txt', 'd1\n')

    def test_list_changes_reads_the_jj_change(self):
        self.describe_change_b()
        out = self.p4son('list-changes', '-b', 'main')
        self.assertIn('1. Change a\n2. Change b\n', out)

    def test_writable_apply_reads_the_jj_change(self):
        self.describe_change_b()
        self.p4son('writable', 'apply')
        self.assertFalse(os.stat(os.path.join(
            self.ws, 'd.txt')).st_mode & stat.S_IWUSR)

    def test_git_backend_follows_git_in_a_jj_workspace(self):
        self.p4son('new', '-b', 'main')
        self.assertEqual(self.p4son('alias', 'show', 'branch'), '101\n')
        # jj leaves git's HEAD detached, so git has no branch to name the alias.
        self.p4son_fails('--backend', 'git', 'alias', 'show',
                         'branch', message='detached HEAD')
        self.write_config(
            '[depot]\nroot = "//myclient"\n\n[core]\nbackend = "git"\n')
        self.p4son_fails('alias', 'show', 'branch', message='detached HEAD')


class TestJjPerCommit(JjP4RepoTestCase):
    """review over Change a and Replace b with c, with bookmark feat on the second; TestReviewOnJj covers the rest."""

    FILES = {'a.txt': 'a2\n', 'b.txt': None, 'c.txt': 'c1\n'}

    def build_workspace(self):
        super().build_workspace()
        self.jj('new', 'main', '-m', 'Change a')
        self.edit('a.txt', 'a2\n')
        self.jj('new', '-m', 'Replace b with c')
        self.replace_b_with_c()
        self.jj('bookmark', 'create', 'feat', '-r', '@')
        self.jj('new')

    def setUp(self):
        super().setUp()
        start_patch(self, 'git_p4son.per_commit.time.sleep')

    def test_a_squash_into_an_included_change_is_opened_by_the_next_update(self):
        """jj squash keeps the change ID, so with nothing new update opens every file since the base."""
        self.p4son('new', '-b', 'main', '--no-edit')
        self.write_file('n.txt', 'n1\n')
        self.jj('squash')
        self.p4son('update', 'feat')
        self.assertIn('n.txt', self.p4.opened)

    def test_an_abandoned_change_warns_and_the_new_one_is_still_included(self):
        self.p4son('new', '-b', 'main', '--no-edit')
        self.jj('abandon', '-r', 'feat-')
        self.add_d()
        out = self.p4son('update', 'feat')
        self.assertIn(
            'dropped or squashed into another, of the 2 commits CL 101 included: number 1.', out)
        self.assertEqual(self.p4.pending['101'][-1], '3. Add d')

    def test_a_new_change_below_included_ones_is_refused(self):
        self.p4son('new', '-b', 'main', '--no-edit')
        self.jj('new', 'main', '-m', 'Add x')
        self.jj('rebase', '-s', 'feat-', '-d', '@')
        self.jj('new', 'feat')
        self.p4son_fails(
            'update', 'feat', message='"Add x" sits below commits CL 101 already includes')

    def test_update_takes_a_jj_revision_as_its_base(self):
        """-b is the backend's: a change ID resolves through jj, as it does without a record."""
        self.p4son('new', '-b', 'main', '--no-edit')
        self.p4son('update', 'feat', '-b', self.describe('main', 'change_id'))
        self.assertEqual(set(self.p4.opened), {'a.txt', 'b.txt', 'c.txt'})

    def add_d(self):
        """Describe the working change as Add d, adding d.txt; returns its commit."""
        self.jj('describe', '-m', 'Add d')
        self.write_file('d.txt', 'd1\n')
        return self.working()

    def assert_restored(self, working, files):
        """Assert jj sees the working change unchanged, with files on disk, None for an absent one."""
        self.assertEqual(self.working(), working)
        for path, content in files.items():
            self.assertEqual(os.path.exists(os.path.join(
                self.ws, path)), content is not None, path)
            if content is not None:
                self.assertEqual(self.read(path), content)

    def test_review_records_the_change_it_started_from(self):
        working = self.working()
        self.p4son('review', 'branch', '-m', 'Feature', '-b', 'main')
        self.assertEqual(len(self.shelves()), 2)
        self.assertEqual(self.p4son('alias', 'list').splitlines()[-1],
                         f'feat -> CL 101, jj change {self.describe("feat", "change_id")[:8]}')
        self.assert_restored(working, self.FILES)

    def test_review_takes_the_working_change_and_puts_its_files_back(self):
        working = self.add_d()
        self.p4son('review', 'feat', '-m', 'Feature', '-b', 'main')
        self.assertEqual(self.p4.pending['101'][3:6],
                         ['1. Change a', '2. Replace b with c', '3. Add d'])
        self.assertEqual(self.p4.shelved['101']['d.txt'], 'd1\n')
        self.assertEqual(len(self.shelves()), 3)
        self.assert_restored(working, {**self.FILES, 'd.txt': 'd1\n'})

    def test_a_failing_step_puts_the_working_change_back(self):
        working = self.add_d()
        start_patch(self, 'git_p4son.cli.update_command',
                    side_effect=CommandError('p4 said no'))
        self.p4son_fails('review', 'feat', '-m', 'Feature', '-b', 'main',
                         message=r'Step 2 of 3 failed at \w+ Replace b with c: p4 said no')
        self.assert_restored(working, {**self.FILES, 'd.txt': 'd1\n'})

    def test_a_failing_restore_keeps_the_step_error_and_names_the_fix(self):
        working = self.working()
        start_patch(self, 'git_p4son.cli.update_command',
                    side_effect=CommandError('p4 said no'))
        start_patch(self, 'git_p4son.jj.Workspace.restore_working',
                    side_effect=RunError('git read-tree failed'))
        head = self.describe('@-', 'commit_id')
        out = self.p4son_fails('review', 'feat', '-m', 'Feature', '-b', 'main',
                               message='Step 2 of 2 failed at .*: p4 said no')
        self.assertIn('git read-tree failed', out)
        self.assertRegex(out, rf'git update-ref --no-deref HEAD {head}\n'
                         rf'git( -c core\.autocrlf=\w+)? read-tree -u --reset {working}\n'
                         rf'git read-tree {head}\n')

    def test_a_failing_restore_after_the_steps_names_the_fix(self):
        start_patch(self, 'git_p4son.jj.Workspace.restore_working',
                    side_effect=RunError('git read-tree failed'))
        head = self.describe('@-', 'commit_id')
        out = self.p4son_fails('review', 'feat', '-m', 'Feature', '-b', 'main',
                               message='git read-tree failed')
        self.assertEqual(len(self.shelves()), 2)
        self.assertIn(f'git update-ref --no-deref HEAD {head}\n', out)

    def test_a_file_the_working_change_deletes_stays_deleted(self):
        self.jj('describe', '-m', 'Drop a')
        os.chmod(os.path.join(self.ws, 'a.txt'), stat.S_IRUSR | stat.S_IWUSR)
        os.remove(os.path.join(self.ws, 'a.txt'))
        working = self.working()
        self.p4son('review', 'feat', '-m', 'Feature', '-b', 'main')
        self.assertEqual(self.p4.shelved['101']['a.txt'], None)
        self.assert_restored(working, {**self.FILES, 'a.txt': None})


class TestJjSync(JjP4RepoTestCase):
    def build_workspace(self):
        super().build_workspace()
        self.jj('new', 'main')

    def test_sync_moves_the_bookmark_to_each_sync_commit(self):
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.p4.submit(102, {'c.txt': 'c1\n'})
        out = self.p4son('sync', '101', '102')
        self.assertNotIn('Rebasing', out)
        self.assertEqual(self.describe('main'),
                         'git-p4son: p4 sync //myclient/...@102')
        self.assertEqual(self.describe('main-'),
                         'git-p4son: p4 sync //myclient/...@101')
        self.assertEqual(self.describe('@-', 'commit_id'),
                         self.describe('main', 'commit_id'))
        self.assertEqual(self.read('c.txt'), 'c1\n')

    def test_verbose_logs_the_pinned_operation(self):
        with mock.patch.object(log, 'verbose_mode', True):
            out = self.p4son('list-changes', '-b', 'main')
        self.assertRegex(out, r'Pinned jj operation [0-9a-f]{20,}')

    def test_a_stray_file_becomes_a_change_sync_rebases(self):
        """jj snapshots a stray file into the working change, which sync then rebases onto the new sync commit."""
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.write_file('stray.txt', 'x')
        self.p4son('sync', '101')
        self.assertEqual(self.describe(
            '@-'), 'git-p4son: p4 sync //myclient/...@101')
        self.assertEqual(self.read('stray.txt'), 'x')
        self.assertEqual(self.read('a.txt'), 'a2\n')
        # jj marks a file its change adds as intent-to-add in git's index, which keeps it out of the sync commit.
        self.assertNotIn('stray.txt', self.git(
            'ls-tree', '--name-only', 'main').split())

    def test_sync_after_jj_rewrote_a_file_git_indexed_with_crlf(self):
        """git's index may record a file's size from a CRLF copy jj has since rewritten as LF without telling git.

        git then calls the file modified without comparing contents, as Git for Windows' core.autocrlf=true makes
        likely; sync's clean check must compare the files with HEAD instead."""
        path = os.path.join(self.ws, 'a.txt')
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
        self.git('config', 'core.autocrlf', 'true')
        with open(path, 'wb') as f:
            f.write(b'a1\r\n')
        self.git('add', 'a.txt')
        with open(path, 'wb') as f:
            f.write(b'a1\n')
        # On Windows, b.txt can already be in the same state from the setup.
        self.assertIn(['M', 'a.txt'], [line.split()
                      for line in self.git('status', '--porcelain').splitlines()])
        self.p4.submit(101, {'b.txt': 'b2\n'})
        self.p4son('sync', '101')
        self.assertEqual(self.describe('main'),
                         'git-p4son: p4 sync //myclient/...@101')

    def test_a_described_working_change_is_rebased_and_returned_to(self):
        self.jj('describe', '-m', 'work in progress')
        self.edit('b.txt', 'b2\n')
        self.p4.submit(101, {'c.txt': 'c1\n'})
        self.p4son('sync')
        self.assertEqual(self.describe('@'), 'work in progress')
        self.assertEqual(self.describe(
            '@-'), 'git-p4son: p4 sync //myclient/...@101')
        self.assertEqual(
            (self.read('b.txt'), self.read('c.txt')), ('b2\n', 'c1\n'))


class TestJjSyncFromAnywhere(JjP4RepoTestCase):
    """sync from a stack: Change a with bookmark feat on main, and an empty working change above it."""

    def build_workspace(self):
        super().build_workspace()
        self.jj('new', 'main', '-m', 'Change a')
        self.edit('a.txt', 'a2\n')
        self.jj('bookmark', 'create', 'feat', '-r', '@')
        self.jj('new')

    def test_the_stack_is_rebased_onto_the_sync_commit_and_returned_to(self):
        self.p4.submit(101, {'d.txt': 'd1\n'})
        out = self.p4son('sync')
        self.assertEqual(self.describe('main'),
                         'git-p4son: p4 sync //myclient/...@101')
        self.assertEqual(self.describe('feat-'),
                         'git-p4son: p4 sync //myclient/...@101')
        self.assertEqual(self.describe('@-'), 'Change a')
        self.assertEqual(
            (self.read('a.txt'), self.read('d.txt')), ('a2\n', 'd1\n'))
        self.assertIn('main is now at CL 101', out)

    def test_editing_a_change_returns_to_it(self):
        self.jj('edit', 'feat')
        self.p4.submit(101, {'d.txt': 'd1\n'})
        self.p4son('sync')
        self.assertEqual(self.describe('@'), 'Change a')
        self.assertEqual(self.describe(
            '@-'), 'git-p4son: p4 sync //myclient/...@101')

    def test_no_rebase_leaves_the_stack_where_it_was(self):
        self.p4.submit(101, {'d.txt': 'd1\n'})
        out = self.p4son('sync', '--no-rebase')
        self.assertRegex(
            out, r'Run "jj rebase -b \w+ -d main" to bring your changes up to date')
        self.assertEqual(self.describe('main'),
                         'git-p4son: p4 sync //myclient/...@101')
        self.assertEqual(self.describe('feat-'), 'Add config')
        self.assertEqual(self.describe('@-'), 'Change a')
        self.assertFalse(os.path.exists(os.path.join(self.ws, 'd.txt')))

    def test_conflicts_from_the_rebase_are_listed(self):
        self.p4.submit(101, {'a.txt': 'a-perforce\n'})
        out = self.p4son('sync')
        self.assertIn('jj recorded conflicts in the changes above', out)
        self.assertIn('Change a', out.partition('Rebasing your changes')[2])
        self.assertEqual(self.describe('main'),
                         'git-p4son: p4 sync //myclient/...@101')

    def test_a_failing_rebase_returns_you_then_names_the_command(self):
        real = run

        def failing_rebase(command, *args, **kwargs):
            if command[:4] == ['jj', '--no-pager', '--color', 'never'] and 'rebase' in command:
                raise RunError(' '.join(command), 1, ['rebase failed'])
            return real(command, *args, **kwargs)
        self.p4.submit(101, {'d.txt': 'd1\n'})
        with mock.patch('git_p4son.jj.run', side_effect=failing_rebase):
            self.p4son_fails('sync', message=r'main reached CL 101, but jj could not rebase your changes.*'
                             r'Run: jj rebase -b \w+ -d main')
        self.assertEqual(self.describe('main'),
                         'git-p4son: p4 sync //myclient/...@101')
        self.assertEqual(self.describe('@-'), 'Change a')

    def test_a_failure_partway_rebases_nothing(self):
        for change in (101, 102):
            self.p4.submit(change, {'d.txt': f'd{change}\n'})
        real = self.p4.stream

        def fail_second_sync(command, *args, **kwargs):
            if command[-1].endswith('@102') and '-n' not in command:
                raise P4SyncAbortError('cannot replace d.txt')
            return real(command, *args, **kwargs)
        start_patch(self, 'git_p4son.perforce.run_with_stream_output',
                    side_effect=fail_second_sync)
        out = self.p4son_fails('sync', '101', '102', message='Aborting sync')
        self.assertIn('Staying on main', out)
        # The user's next jj command imports what the failed sync left, as this one does.
        self.jj('status')
        self.assertEqual(self.describe('main'),
                         'git-p4son: p4 sync //myclient/...@101')
        self.assertEqual(self.describe(
            '@-'), 'git-p4son: p4 sync //myclient/...@101')
        self.assertEqual(self.describe('feat-'), 'Add config')

    def test_a_failing_pre_sync_hook_puts_the_working_change_back(self):
        # Editing Change a, so git's view, HEAD and index at its parent, shows a.txt as an unstaged change.
        self.jj('edit', 'feat')
        working = self.describe('@', 'commit_id')
        self.hook('pre-sync', 'raise SystemExit(1)\n')
        self.p4.submit(101, {'d.txt': 'd1\n'})
        self.p4son_fails('sync', message='pre-sync hook failed')
        self.assertEqual(self.p4.synced(), [])
        self.assertEqual(self.read('a.txt'), 'a2\n')
        self.assertEqual(self.git('status', '--porcelain'), 'M a.txt')
        self.assertEqual(self.working(), working)

    def test_merged_back_changes_stay_on_the_sync_bookmark_and_the_stack_is_rebased(self):
        self.jj('new', 'main', '-m', 'local change')
        self.edit('b.txt', 'local\nb1\n')
        self.jj('bookmark', 'set', 'main', '-r', '@')
        self.jj('new', 'feat')
        self.p4.submit(101, {'b.txt': 'b1\nupstream\n'})
        out = self.p4son('sync')
        self.assertIn(
            'Staying on main, with the merged local changes in the working change', out)
        self.assertEqual(self.describe(
            '@-'), 'git-p4son: p4 sync //myclient/...@101')
        self.assertEqual(self.read('b.txt'), 'local\nb1\nupstream\n')
        self.assertEqual(self.describe('feat-'),
                         'git-p4son: p4 sync //myclient/...@101')

    def test_the_setting_names_the_sync_bookmark(self):
        self.jj('bookmark', 'create', 'p4', '-r', 'main')
        self.write_config(
            '[depot]\nroot = "//myclient"\n\n[sync]\nbranch = "p4"\n')
        self.p4.submit(101, {'d.txt': 'd1\n'})
        self.p4son('sync')
        self.assertEqual(self.describe(
            'p4'), 'git-p4son: p4 sync //myclient/...@101')
        self.assertEqual(self.describe('main'), 'Add config')


if __name__ == '__main__':
    unittest.main()
