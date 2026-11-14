"""Tests for git_p4son.jj and git_p4son.jj_lock modules."""

import dataclasses
import os
import shlex
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
from tests.helpers import (
    captured_output, CRLF_WHITELIST_GITIGNORE, isolate_jj, jj_tracked, JjTestCase, make_run_result, P4RepoTestCase,
    recorded, requires_jj, run_jj, save_config, start_patch, WorkspaceTestCase,
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

    def git(self, *args):
        subprocess.run(['git', *args], cwd=self.ws, check=True)

    def output(self, **kwargs):
        """The output of a jj.operation that does nothing."""
        with captured_output() as out, jj.operation(self.ws, **kwargs):
            pass
        return out.getvalue()

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

    def test_a_bookmark_names_itself_and_its_commit_and_a_change_without_one_its_change_id(self):
        with self.operation() as workspace:
            backend = JjBackend(workspace)
            self.assertEqual(backend.base_name('main'), 'main')
            self.assertEqual(backend.base_name(self.commit_of('main')), 'main')
            self.assertEqual(backend.base_name(
                '@'), f'change_id({workspace.working.change_id})')

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

    def test_a_change_on_the_sync_bookmark_passes_the_sync_tip_check(self):
        with self.operation() as workspace:
            JjBackend(workspace).check_on_sync_tip()
            JjBackend(workspace).check_on_sync_tip([self.commit_of('feat')])

    def test_a_change_the_sync_bookmark_moved_on_from_is_refused(self):
        self.jj('new', 'main', '-m', 'sync')
        self.jj('bookmark', 'set', 'main', '-r', '@')
        self.jj('new', 'feat')
        commit = self.commit_of('feat')
        with self.operation() as workspace:
            with self.assertRaisesRegex(CommandError, 'The change is not based on the latest main, .* Rebase it onto '
                                        'main, or onto a bookmark that is rebased on it.'):
                JjBackend(workspace).check_on_sync_tip()
            with self.assertRaisesRegex(CommandError, f'Commit {commit[:7]} is not based on the latest main'):
                JjBackend(workspace).check_on_sync_tip([commit])

    def test_without_the_sync_bookmark_there_is_nothing_to_check(self):
        self.jj('bookmark', 'delete', 'main')
        with self.operation() as workspace:
            JjBackend(workspace).check_on_sync_tip()

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
    def test_mismatch_is_reported_with_the_fix(self):
        self.git('config', 'core.autocrlf', 'true')
        out = self.output()
        self.assertIn(
            "jj's working-copy.eol-conversion is none, but git's core.autocrlf is true", out)
        self.assertIn(
            'jj config set --repo working-copy.eol-conversion input-output', out)

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
        self.assertNotIn('write line endings differently',
                         self.output(lock_only=True))


class TestIgnoredCheck(JjRepoTestCase):
    """git ignores ThirdParty/ case-insensitively, as with core.ignorecase, and jj matches it with case."""

    def setUp(self):
        super().setUp()
        self.git('config', 'core.ignorecase', 'true')
        self.write_file('.gitignore', 'ThirdParty/\n')

    def run_fix(self, out):
        """Run the jj commands the warning names, then snapshot."""
        for line in out.splitlines():
            if line.startswith('jj '):
                self.jj(*shlex.split(line)[1:])
        self.jj('status')

    def assert_fixed(self, out, *paths):
        self.run_fix(out)
        tracked = jj_tracked(self.ws)
        for path in paths:
            self.assertNotIn(path, tracked)
            self.assertTrue(os.path.exists(os.path.join(self.ws, path)))

    def test_a_file_jj_tracks_and_git_ignores_is_reported_with_the_fix(self):
        self.write_file('Src/Thirdparty/lib.c', 'x\n')
        out = self.output()
        self.assertIn(
            'jj tracks 1 file that git ignores only because of core.ignorecase', out)
        # Double quotes, which cmd, PowerShell and POSIX shells all read alike.
        self.assertIn(
            'jj config set --repo snapshot.auto-track "(all()) ~ root:Src/Thirdparty"\n', out)
        self.assertIn('jj file untrack root:Src/Thirdparty\n', out)
        self.assert_fixed(out, 'Src/Thirdparty/lib.c')
        self.assertEqual(self.jj(
            'config', 'get', 'snapshot.auto-track'), '(all()) ~ root:Src/Thirdparty')

    def test_a_folder_whose_name_starts_another_s_is_still_added(self):
        self.jj('config', 'set', '--repo', 'snapshot.auto-track',
                'all() ~ root:Src/Thirdparty2')
        self.write_file('Src/Thirdparty/lib.c', 'x\n')
        self.run_fix(self.output())
        self.assertEqual(self.jj('config', 'get', 'snapshot.auto-track'),
                         '(all() ~ root:Src/Thirdparty2) ~ root:Src/Thirdparty')

    def test_a_path_with_a_space_is_quoted_in_the_fileset(self):
        self.write_file('C D/Thirdparty/lib.c', 'x\n')
        out = self.output()
        self.assert_fixed(out, 'C D/Thirdparty/lib.c')
        self.assertEqual(self.jj(
            'config', 'get', 'snapshot.auto-track'), '(all()) ~ root:"C D/Thirdparty"')

    def test_the_tip_on_the_sync_branch_is_git_s_and_not_checked(self):
        """A file git tracks there, as added with git add -f, is git's, not one a jj snapshot took."""
        self.write_file('Src/Thirdparty/forced.c', 'f\n')
        self.jj('commit', '-m', 'forced')
        self.jj('bookmark', 'set', 'main', '-r', '@-', '--allow-backwards')
        self.assertNotIn('only because of core.ignorecase', self.output())

    def test_the_fix_builds_on_an_auto_track_setting_of_the_user_s(self):
        self.jj('config', 'set', '--repo', 'snapshot.auto-track',
                '"all() ~ glob:\'*.tmp\'"')
        self.write_file('Src/Thirdparty/lib.c', 'x\n')
        self.run_fix(self.output())
        self.assertEqual(self.jj('config', 'get', 'snapshot.auto-track'),
                         "(all() ~ glob:'*.tmp') ~ root:Src/Thirdparty")

    def test_a_whitelist_gitignore_with_crlf_still_names_the_folder(self):
        """git's check-ignore calls any folder ignored there, and the re-includes outrank .git/info/exclude."""
        with open(os.path.join(self.ws, '.gitignore'), 'wb') as f:
            f.write(CRLF_WHITELIST_GITIGNORE)
        self.write_file(
            'Some/Plugins/A/Source/Thirdparty/SDK/version.txt', 'v\n')
        self.write_file(
            'Some/Plugins/A/Source/Thirdparty/docs/index.txt', 'i\n')
        out = self.output()
        self.assertIn('jj tracks 2 files', out)
        self.assertIn(
            'jj file untrack root:Some/Plugins/A/Source/Thirdparty\n', out)
        self.assert_fixed(out, 'Some/Plugins/A/Source/Thirdparty/SDK/version.txt',
                          'Some/Plugins/A/Source/Thirdparty/docs/index.txt')

    def test_a_folder_holding_files_git_tracks_is_named_file_by_file(self):
        """Untracking the folder would untrack old.c too, which jj would then record as deleted."""
        self.write_file('Src/Thirdparty/old.c', 'o\n')
        self.jj('commit', '-m', 'old')
        self.write_file('Src/Thirdparty/new.c', 'n\n')
        out = self.output()
        self.assertIn('jj file untrack root:Src/Thirdparty/new.c\n', out)
        self.assert_fixed(out, 'Src/Thirdparty/new.c')
        self.assertIn('Src/Thirdparty/old.c', jj_tracked(self.ws))

    def test_a_file_moved_out_of_the_working_change_by_jj_new_is_still_reported(self):
        """jj new leaves the files in the tip, the change git-p4son sends."""
        self.write_file('Src/Thirdparty/lib.c', 'x\n')
        self.jj('new')
        self.assertIn('root:Src/Thirdparty', self.output())

    def test_a_file_git_ignores_with_case_too_is_quiet(self):
        """Tracked before generated/ was ignored, as jj file track --include-ignored would."""
        self.write_file('generated/keep.txt', 'k\n')
        self.jj('status')
        self.write_file('.gitignore', 'ThirdParty/\ngenerated/\n')
        self.assertNotIn('only because of core.ignorecase', self.output())

    def test_a_command_that_doesn_t_snapshot_doesn_t_check(self):
        self.write_file('Src/Thirdparty/lib.c', 'x\n')
        self.jj('status')
        self.assertNotIn('only because of core.ignorecase',
                         self.output(snapshot=False))

    def test_files_git_ignores_too_are_quiet(self):
        self.write_file('Src/ThirdParty/lib.c', 'x\n')
        self.assertNotIn('only because of core.ignorecase', self.output())


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

    def test_clean_check_is_one_section_closed_once_both_checks_pass(self):
        with self.operation() as workspace, captured_output() as out:
            workspace.check_clean()
        self.assertRegex(out.getvalue(), r'# Checking jj workspace files\n(>  .*\n)*>  git .*diff --name-only.*\n'
                         r'\[ok\] All files snapshotted, none changed since\n$')

    def test_clean_check_refuses_a_file_changed_after_the_snapshot(self):
        """new and update open the files on disk, which would then differ from the change they read."""
        self.write_file('x.txt', 'x1\n')
        self.jj('status')
        with open_backend(self.ws, 'jj') as backend, captured_output():
            self.write_file('x.txt', 'x2\n')
            with self.assertRaisesRegex(CommandError, 'changed after jj snapshotted'):
                backend.check_clean()

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

    def working(self):
        """The working change's commit, after jj snapshots the files on disk."""
        self.jj('status')
        return self.describe('@', 'commit_id')

    def change(self, message, path, content, parent=None):
        """Start a change on parent, or on the working change, that edits path; returns its change ID."""
        self.jj('new', *([parent] if parent else []), '-m', message)
        self.edit(path, content)
        return self.describe('@', 'change_id')

    def bookmark(self, name):
        self.jj('bookmark', 'set', name, '-r', '@')


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

    def test_new_again_under_the_alias_in_another_case_is_the_same_alias(self):
        self.p4son('new', 'Other', '--no-edit', '-b', 'main')
        self.p4son_fails('new', 'OTHER', '--no-edit', '-b', 'main',
                         message='Alias "other" already exists')
        self.p4son('new', 'OTHER', '--no-edit', '--force', '-b', 'main')
        self.assertEqual(alias_changes(self.ws), {'other': self.feat_change()})

    def test_alias_new_records_no_change(self):
        self.p4.pending['555'] = ['Made elsewhere']
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

    def test_alias_clean_keeps_an_alias_whose_change_is_gone_but_whose_changelist_is_pending(self):
        self.p4son('new', '-b', 'main')
        change = self.feat_change()
        self.jj('abandon', 'feat')
        self.assertIn('# feat -> CL 101\n[ok] Kept, CL 101 is pending\n',
                      self.p4son('alias', 'clean'))
        self.assertEqual(alias_changes(self.ws), {'feat': change})

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
        self.assertEqual(self.p4son('list-changes', '-b', 'main', '--porcelain'),
                         '1. Change a\n2. Change b\n')

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


class TestJjAliasInChain(JjP4RepoTestCase):
    """The branch keyword on a tip whose own change no alias records, but one further down the chain does."""

    def test_an_alias_named_by_change_id_is_found_under_a_bookmark_added_later(self):
        widget = self.change('Add widget', 'a.txt', 'a2\n', parent='main')
        self.p4son('new', '-b', 'main')
        self.change('Handle errors', 'b.txt', 'b2\n')
        self.change('Polish', 'c.txt', 'c1\n')
        self.bookmark('my-feature')
        self.jj('new')
        self.assertEqual(self.p4son('alias', 'show'), '101\n')
        self.assertIn(f'branch -> {widget[:8]}',
                      self.p4son('update', '--no-edit'))

    def test_a_stack_of_changes_without_bookmarks_finds_the_alias_below(self):
        widget = self.change('Add widget', 'a.txt', 'a2\n', parent='main')
        self.p4son('new', '-b', 'main')
        self.change('Handle errors', 'b.txt', 'b2\n')
        self.change('Polish', 'c.txt', 'c1\n')
        self.jj('new')
        self.assertEqual(self.p4son('alias', 'show'), '101\n')
        self.assertIn(f'branch -> {widget[:8]}',
                      self.p4son('update', '--no-edit'))

    def test_an_emptied_recorded_change_is_skipped(self):
        # After a submit and a sync, the rebase onto main leaves the submitted changes empty.
        old = self.change('Old work', 'a.txt', 'a2\n', parent='main')
        self.p4son('new', '-b', 'main')
        self.jj('restore', '--changes-in', old)
        self.change('Next work', 'b.txt', 'b2\n')
        self.jj('new')
        self.p4son_fails('alias', 'show', message='No changelist alias found')

    def test_the_nearest_of_two_recorded_changes_wins(self):
        self.change('A: code', 'a.txt', 'a2\n', parent='main')
        self.bookmark('base-of-b')
        self.p4son('new', 'a-cl', '-b', 'main')
        self.change('B: code', 'b.txt', 'b2\n')
        self.p4son('new', 'b-cl', '-b', 'base-of-b')
        # Without a bookmark between them, both recorded changes are in the tip's chain.
        self.jj('bookmark', 'delete', 'base-of-b')
        self.change('B: tests', 'c.txt', 'c1\n')
        self.bookmark('feature-b')
        self.jj('new')
        self.assertEqual(self.p4son('alias', 'show'), '102\n')

    def test_a_bookmark_below_the_tip_stops_the_search(self):
        # B is stacked on A, but has no CL of its own yet: A's alias must not stand in for it.
        self.change('A: code', 'a.txt', 'a2\n', parent='main')
        self.bookmark('feature-a')
        self.p4son('new', '-b', 'main')
        self.change('B: code', 'b.txt', 'b2\n')
        self.bookmark('feature-b')
        self.jj('new')
        self.p4son_fails(
            'alias', 'show', message='No changelist alias found: feature-b')

    def test_a_bookmark_above_the_recorded_change_stops_the_search(self):
        # Old work left in the chain, its alias on a change below its bookmark, with new work on top.
        self.change('Old: code', 'a.txt', 'a2\n', parent='main')
        self.p4son('new', 'old-work', '-b', 'main')
        self.change('Old: polish', 'b.txt', 'b2\n')
        self.bookmark('old-feature')
        self.change('Next: code', 'c.txt', 'c1\n')
        self.bookmark('next-thing')
        self.jj('new')
        self.p4son_fails(
            'alias', 'show', message='No changelist alias found: next-thing')

    def test_a_divergent_change_another_alias_records_doesnt_break_the_lookup(self):
        other = self.change('Other work', 'a.txt', 'a2\n', parent='main')
        self.p4son('new', 'other', '-b', 'main')
        # Rewriting the change at an earlier operation too leaves two revisions of it: a divergent change.
        earlier = self.jj('op', 'log', '--no-graph', '-n', '1', '-T', 'id')
        self.jj('describe', '-r', other, '-m', 'Other work, reworded')
        self.jj('--at-op', earlier, 'describe', '-r', other,
                '-m', 'Other work, reworded elsewhere')
        self.change('Mine', 'b.txt', 'b2\n', parent='main')
        self.bookmark('mine')
        self.jj('new')
        self.p4son_fails(
            'alias', 'show', message='No changelist alias found: mine')

    def test_a_bookmark_left_behind_hides_the_alias_until_moved_to_the_tip(self):
        self.change('Add widget', 'a.txt', 'a2\n', parent='main')
        self.bookmark('my-feature')
        self.p4son('new', '-b', 'main')
        self.change('Handle errors', 'b.txt', 'b2\n')
        self.jj('new')
        self.p4son_fails('alias', 'show', message='No changelist alias found')
        self.jj('bookmark', 'move', 'my-feature', '--to', '@-')
        self.assertEqual(self.p4son('alias', 'show'), '101\n')

    def test_two_aliased_chains_merged_fall_back_to_the_tip(self):
        self.change('X', 'a.txt', 'a2\n', parent='main')
        self.p4son('new', 'x', '-b', 'main')
        x = self.describe('@', 'commit_id')
        self.change('Y', 'b.txt', 'b2\n', parent='main')
        self.p4son('new', 'y', '-b', 'main')
        y = self.describe('@', 'commit_id')
        self.jj('new', x, y, '-m', 'Merge')
        self.bookmark('merged')
        self.jj('new')
        self.p4son_fails(
            'alias', 'show', message='No changelist alias found: merged')


class TestJjChangeBase(JjP4RepoTestCase):
    """-b naming a jj change without a bookmark: a local change kept under feat, never submitted."""

    def build_workspace(self):
        super().build_workspace()
        self.local = self.change(
            'local: ignore tool output', 'local.txt', 'local\n', parent='main')
        self.change('Change a', 'a.txt', 'a2\n')
        self.bookmark('feat')
        self.jj('new')

    def base(self):
        return recorded('101', self.ws).base

    def test_new_records_a_change_without_a_bookmark_by_its_change_id(self):
        self.p4son('new', '-b', self.local[:8])
        self.assertEqual(self.base(), f'change_id({self.local})')
        self.assertEqual(self.p4.pending['101'], [
                         'Change a', '', 'Changes included:', '1. Change a'])
        self.assertEqual(self.p4.opened, {'a.txt': ('101', 'edit')})

    def test_update_finds_the_base_change_after_it_is_rewritten(self):
        self.p4son('new', '-b', self.local[:8])
        self.jj('describe', '-r', self.local, '-m',
                'local: ignore more tool output')
        self.change('Change b', 'b.txt', 'b2\n', parent='feat')
        self.bookmark('feat')
        self.jj('new')
        self.p4son('update')
        self.assertEqual(self.p4.pending['101'][-1], '2. Change b')
        self.assertNotIn('local.txt', self.p4.opened)

    def test_a_gone_base_change_refuses_update_and_names_the_fix(self):
        self.p4son('new', '-b', self.local[:8])
        self.jj('abandon', self.local)
        self.p4son_fails(
            'update', message=rf'change_id\({self.local}\).*git p4son update feat -b <base>')

    def test_update_with_b_records_a_new_base_for_a_gone_one(self):
        self.p4son('new', '-b', self.local[:8])
        self.jj('abandon', self.local)
        self.p4son('update', '-b', 'main')
        self.assertEqual(self.base(), 'main')
        self.assertEqual(self.p4.pending['101'][-1], '1. Change a')

    def test_after_a_lost_base_b_at_the_cl_s_own_commit_refuses(self):
        self.p4son('new', '-b', self.local[:8])
        self.jj('abandon', self.local)
        self.change('Change b', 'b.txt', 'b2\n', parent='feat')
        self.bookmark('feat')
        self.jj('new')
        self.p4son_fails('update', '-b', 'feat-',
                         message=r'includes \(number 1\).*below its first commit: git p4son update feat -b <base>')
        self.assertEqual(self.base(), f'change_id({self.local})')

    def test_review_records_a_change_base_for_every_step(self):
        start_patch(self, 'git_p4son.per_commit.time.sleep')
        self.change('Change b', 'b.txt', 'b2\n', parent='feat')
        self.bookmark('feat')
        self.jj('new')
        self.p4son('review', '-m', 'Feature', '-b', self.local[:8])
        self.assertEqual(self.base(), f'change_id({self.local})')
        self.assertEqual(self.p4.pending['101'][2:5], [
                         'Changes included:', '1. Change a', '2. Change b'])
        self.assertNotIn('local.txt', self.p4.shelved['101'])
        self.assertEqual(len(self.shelves()), 2)

    def test_a_divergent_base_change_refuses_update(self):
        self.p4son('new', '-b', self.local[:8])
        # Diverging a change diverges its descendants too, the tip with them, so move the feature off it first.
        self.jj('rebase', '-s', 'feat', '-d', 'main')
        earlier = self.jj('op', 'log', '--no-graph', '-n', '1', '-T', 'id')
        self.jj('describe', '-r', self.local, '-m', 'local, reworded')
        self.jj('--at-op', earlier, 'describe', '-r',
                self.local, '-m', 'local, reworded elsewhere')
        # A plain jj command merges the two operations.
        self.jj('status')
        self.p4son_fails(
            'update', message=r'which no longer resolves.*git p4son update feat -b <base>')

    def test_a_revision_with_a_bookmark_still_records_the_bookmark(self):
        self.p4son('new', '-b', 'main')
        self.assertEqual(self.base(), 'main')


class TestJjBaseFromBookmarkBelow(JjP4RepoTestCase):
    """new and review without -b take the nearest bookmark below the change."""

    def stack(self):
        """Stack high on low on main, with a bookmark each, and an empty working change on top."""
        self.change('Change a', 'a.txt', 'a2\n', parent='main')
        self.bookmark('low')
        self.change('Change b', 'b.txt', 'b2\n')
        self.bookmark('high')
        self.jj('new')

    def test_a_change_on_the_sync_bookmark_takes_it(self):
        self.change('Change a', 'a.txt', 'a2\n', parent='main')
        self.bookmark('feat')
        self.jj('new')
        out = self.p4son('new', '-m', 'Feature')
        self.assertIn(
            f'[ok] main -> {self.describe("main", "commit_id")} (nearest bookmark below)\n', out)
        self.assertEqual(recorded('101', self.ws).base, 'main')

    def test_a_stacked_change_takes_the_bookmark_below(self):
        self.stack()
        self.p4son('new', '-m', 'Feature')
        self.assertEqual(recorded('101', self.ws).base, 'low')
        self.assertEqual(self.p4.pending['101'], [
                         'Feature', '', 'Changes included:', '1. Change b'])
        self.assertEqual(self.p4.opened, {'b.txt': ('101', 'edit')})

    def test_review_takes_it_too(self):
        self.stack()
        out = self.p4son('review', '-m', 'Feature', '--dry-run')
        self.assertIn('(nearest bookmark below)', out)
        self.assertIn('Would run 1 step', out)

    def test_b_wins_over_the_bookmark_below(self):
        self.stack()
        self.p4son('new', '-m', 'Feature', '-b', 'main')
        self.assertEqual(recorded('101', self.ws).base, 'main')

    def test_a_conflicted_sync_bookmark_is_named_instead_of_a_rebase(self):
        first = self.change('First', 'a.txt', 'a2\n', parent='main')
        second = self.change('Second', 'b.txt', 'b2\n', parent='main')
        operation = self.jj('op', 'log', '-n', '1', '--no-graph', '-T', 'id')
        for change in (first, second):
            self.jj('--at-op', operation, 'bookmark',
                    'set', 'main', '-r', change)
        self.jj('edit', first)
        self.p4son_fails('new', 'feat', '-m', 'Feature', '-b', 'main',
                         message='jj bookmark "main" is conflicted. Resolve it first.')
        self.assertEqual(self.p4.pending, {})

    def assert_in_sync_history_is_refused(self, sync):
        """old..feat would take in the sync commit between them, as the sync bookmark has moved on past it."""
        self.jj('bookmark', 'create', 'old', '-r', sync)
        self.change('Sync', 's.txt', 's1\n', parent=sync)
        self.jj('bookmark', 'set', sync, '-r', '@')
        self.change('Change a', 'a.txt', 'a2\n')
        self.bookmark('feat')
        self.change('Sync 2', 't.txt', 't1\n', parent=sync)
        self.jj('bookmark', 'set', sync, '-r', '@')
        self.jj('new', 'feat')
        self.p4son_fails('new', '-m', 'Feature', '--no-edit',
                         message=f'-b is required, as old, the nearest bookmark below the change, is in the history '
                         f'of {sync}, .* such as {sync}\\.')
        self.assertEqual(self.p4.pending, {})

    def test_a_bookmark_in_the_sync_bookmark_s_history_is_refused(self):
        self.assert_in_sync_history_is_refused('main')

    def test_the_sync_branch_setting_names_the_sync_bookmark(self):
        self.jj('bookmark', 'rename', 'main', 'trunk')
        save_config(self.ws, {'sync': {'branch': 'trunk'}})
        self.assert_in_sync_history_is_refused('trunk')

    def test_no_bookmark_below_needs_b_and_suggests_the_sync_bookmark(self):
        """A change left on an older sync commit, which main has moved on from."""
        self.change('Change a', 'a.txt', 'a2\n', parent='main')
        self.bookmark('feat')
        self.change('Sync', 's.txt', 's1\n', parent='main')
        self.jj('bookmark', 'set', 'main', '-r', '@')
        self.jj('new', 'feat')
        self.p4son_fails('new', '-m', 'Feature', '--no-edit',
                         message=r'no bookmark is below the change. Name the base it starts from, such as main\.')

    def test_a_bookmark_on_an_emptied_change_stays_the_base(self):
        """After low is submitted and synced, its emptied change stays out of high's CL."""
        self.stack()
        self.jj('restore', '--changes-in', 'low')
        self.p4son('new', '-m', 'Feature')
        self.assertEqual(recorded('101', self.ws).base, 'low')
        self.assertEqual(self.p4.pending['101'], [
                         'Feature', '', 'Changes included:', '1. Change b'])

    def test_two_bookmarks_on_the_nearest_commit_need_b(self):
        self.stack()
        self.jj('bookmark', 'create', 'also-low', '-r', 'low')
        self.p4son_fails('new', '-m', 'Feature',
                         message='-b is required, as also-low, low are the nearest bookmarks')
        self.assertEqual(self.p4.pending, {})


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

    def test_the_run_ends_returning_to_the_working_change(self):
        change = self.describe('@', 'change_id')[:8]
        out = self.p4son('review', 'feat', '-m', 'Feature', '-b', 'main')
        self.assertRegex(out, r'\[ok\] 2 commits since \w+\n\n# Checking each commit for the latest main\n'
                         r'(>  .*\n)*\[ok\] Based on it\n\n# Step 1 of 2: ')
        self.assertRegex(out, rf'\n# Returning to jj change {change}\n(>  .*\n)*'
                         r'\[ok\] Ran 2 steps, git p4son new feat --review, then git p4son update feat --shelve\n'
                         r'\n# Reconciling jj workspace\n')

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


@requires_jj
class TestJjCatchUp(JjP4RepoTestCase):
    """CL 102, reviewed from Edit m top with bookmark feat; then CL 103, changing m.txt's last line, is synced."""

    M = ''.join(f'{line}\n' for line in range(1, 9))

    def build_workspace(self):
        super().build_workspace()
        self.commit_sync(101, {'m.txt': self.M})
        self.jj('new', 'main', '-m', 'Edit m top')
        self.edit('m.txt', 'top\n' + self.M.split('\n', 1)[1])
        self.jj('bookmark', 'create', 'feat', '-r', '@')
        self.jj('new')

    def setUp(self):
        super().setUp()
        start_patch(self, 'git_p4son.per_commit.time.sleep')

    def test_the_catch_up_change_goes_in_after_the_working_change_itself(self):
        """With the last included change being @ itself, the catch-up lands above it, outside the change's range."""
        self.p4son('new', '-b', 'main', '--review')
        self.p4.submit(103, {'m.txt': self.M.replace('8\n', 'eight\n')})
        self.p4.opened.clear()
        self.p4son('sync')
        self.jj('edit', 'feat')
        self.p4son('update', 'feat', '--shelve')
        self.assertEqual([line for line in self.p4.pending['102'] if line[:1].isdigit()],
                         ['1. Edit m top', '2. Rebase onto CL 103'])
        self.assertIn('The working copy has no changes.', self.jj('status'))
        # A new change on top, so the next edits don't go into the catch-up.
        self.assertEqual(self.describe('@-'), 'Rebase onto CL 103')
        self.assertEqual(self.describe('@', 'empty'), 'true')

    def test_the_catch_up_change_goes_in_before_the_new_ones(self):
        self.p4son('new', '-b', 'main', '--review')
        self.p4.submit(103, {'m.txt': self.M.replace('8\n', 'eight\n')})
        # Reverted before syncing, as sync asks; the shelf and the review stay.
        self.p4.opened.clear()
        self.p4son('sync')
        self.jj('describe', '-m', 'Add o')
        self.write_file('o.txt', 'o1\n')
        self.p4son('update', 'feat', '--per-commit', '--shelve')
        self.assertEqual([line for line in self.p4.pending['102'] if line[:1].isdigit()],
                         ['1. Edit m top', '2. Rebase onto CL 103', '3. Add o'])
        self.assertEqual(self.describe('feat'), 'Edit m top')
        self.assertEqual(self.describe('feat+'), 'Rebase onto CL 103')
        self.assertEqual(self.read('o.txt'), 'o1\n')


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

    def test_a_folder_git_ignores_only_without_case_is_left_out_of_jj_s_next_snapshots(self):
        self.git('config', 'core.ignorecase', 'true')
        self.p4.submit(101, {'.gitignore': 'ThirdParty/\n',
                       'Src/Thirdparty/lib.c': 'x\n'})
        self.p4son('sync', '101')
        self.assertEqual(self.read('Src/Thirdparty/lib.c'), 'x\n')
        self.assertNotIn('Src/Thirdparty/lib.c', jj_tracked(self.ws))
        self.assertEqual(self.jj(
            'config', 'get', 'snapshot.auto-track'), '(all()) ~ root:Src/Thirdparty')
        self.p4.submit(102, {'a.txt': 'a2\n'})
        self.p4son('sync', '102')
        self.assertEqual(self.jj(
            'config', 'get', 'snapshot.auto-track'), '(all()) ~ root:Src/Thirdparty')

    def test_logs_the_change_and_pinned_operation(self):
        out = self.p4son('list-changes', '-b', 'main')
        self.assertIn('Opening jj workspace', out)
        self.assertRegex(out, r'Change [a-z]{8} at operation [0-9a-f]{12}')

    def test_resolves_the_base_under_a_heading_of_its_own(self):
        out = self.p4son('list-changes', '-b', 'main')
        heading = out.index('# Resolving base')
        self.assertLess(heading, out.index(' log -r main '))
        self.assertIn(
            f'[ok] main -> {self.describe("main", "commit_id")}', out[heading:])

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

    def assert_sync_refused(self, message):
        """Point main at @, then check sync and sync --dry-run refuse, leaving main and @ alone; returns the output."""
        self.jj('bookmark', 'set', 'main', '-r', '@')
        self.p4.submit(101, {'c.txt': 'c1\n'})
        main, working = self.describe('main', 'commit_id'), self.working()
        for argv in ([], ['--dry-run']):
            with self.subTest(argv=argv):
                out = self.p4son_fails('sync', *argv,
                                       message=f'jj bookmark "main" points at the working change.*{message}')
                self.assertEqual(self.p4.synced(), [])
                self.assertEqual(
                    (self.describe('main', 'commit_id'), self.working()), (main, working))
        return out

    def test_sync_refuses_when_the_bookmark_points_at_an_edited_working_change(self):
        self.edit('b.txt', 'stale\n')
        self.write_file('new.txt', 'x')
        out = self.assert_sync_refused('jj describe')
        files = out[out.index('# Checking sync bookmark main'):]
        self.assertIn('~ b.txt', files)
        self.assertIn('+ new.txt', files)

    def test_the_refusal_lists_a_rename_as_a_move(self):
        self.move('b.txt', 'moved/b.txt')
        out = self.assert_sync_refused('jj describe')
        self.assertIn('> b.txt -> moved/b.txt',
                      out[out.index('# Checking sync bookmark main'):])

    def test_sync_refuses_when_the_bookmark_points_at_an_empty_working_change(self):
        self.assert_sync_refused(
            'Move it back first: jj bookmark set main -r @- --allow-backwards')

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

    def test_a_same_size_edit_in_the_second_git_recorded_the_file_stays_in_the_working_change(self):
        # jj keeps git's stat for b1 in the index it writes, and Git for Windows compares mtimes in whole seconds.
        self.jj('describe', '-m', 'work in progress')
        path = os.path.join(self.ws, 'b.txt')
        second = (os.stat(path).st_mtime_ns // 10**9 - 100) * 10**9
        os.utime(path, ns=(second, second + 10**8))
        self.git('update-index', '-q', '--refresh')
        self.edit('b.txt', 'b2\n')
        os.utime(path, ns=(second, second + 5 * 10**8))
        self.p4.submit(101, {'c.txt': 'c1\n'})
        self.p4son('sync')
        self.assertEqual(self.describe('@'), 'work in progress')
        self.assertEqual(self.describe('main+', 'change_id'),
                         self.describe('@', 'change_id'))
        self.assertEqual(
            (self.read('b.txt'), self.read('c.txt')), ('b2\n', 'c1\n'))


class TestJjCherryPick(JjP4RepoTestCase):
    def test_from_a_change_it_moves_the_sync_bookmark_and_rebases_the_change(self):
        self.change('Change c', 'c.txt', 'c1\n', parent='main')
        self.bookmark('feat')
        self.jj('new')
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.p4.submit(102, {'b.txt': 'b2\n'})
        out = self.p4son('cherry-pick', '102')
        self.assertEqual(self.describe('main'),
                         'git-p4son: p4 sync //myclient/...@=102')
        self.assertEqual(self.describe('feat-', 'commit_id'),
                         self.describe('main', 'commit_id'))
        self.assertEqual((self.read('a.txt'), self.read(
            'b.txt'), self.read('c.txt')), ('a1\n', 'b2\n', 'c1\n'))
        self.assertIn('main is now at CL 100 with cherry-picks 102', out)


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
