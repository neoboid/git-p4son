"""Tests for git_p4son.lib module."""

import os
import stat
import unittest
from unittest import mock

from git_p4son.common import RunError
from git_p4son.git import LocalChanges
from git_p4son.lib import (
    create_changelist,
    DRY_RUN_CHANGELIST,
    include_changes_in_changelist,
    revert_unchanged_files,
    update_changelist,
)
from tests.helpers import (
    captured_output,
    make_run_result,
    SAMPLE_SPEC,
    SAMPLE_SPEC_NO_COMMITS,
    start_patch,
    WorkspaceTestCase,
)


def _commands(mock_run):
    """The (command, stdin) of each run() call."""
    return [(c.args[0], c.kwargs.get('input')) for c in mock_run.call_args_list]


SPEC_NO_LIST_WITH_KEYWORD = """\
Change:\t12345

Description:
\tOld title
\t
\t#review-678

Files:
"""

SPEC_LIST_WITH_KEYWORD = """\
Change:\t12345

Description:
\tOld title
\t
\tChanges included:
\t1. Add validation
\t
\t#review-678

Files:
"""


class TestCreateChangelist(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    def test_creates_changelist(self, mock_get_lines, mock_run):
        mock_get_lines.return_value = ['Add feature', 'Fix bug']
        mock_run.return_value = make_run_result(
            stdout=['Change 99999 created.'])
        cl_num = create_changelist(
            'My message', 'HEAD~1', '/ws', dry_run=False)
        self.assertEqual(cl_num, '99999')
        call_kwargs = mock_run.call_args
        spec_input = call_kwargs.kwargs.get('input')
        self.assertIn('My message', spec_input)
        self.assertIn('1. Add feature', spec_input)

    @mock.patch('git_p4son.perforce.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    def test_no_commits(self, mock_get_lines, mock_run):
        mock_get_lines.return_value = []
        mock_run.return_value = make_run_result(
            stdout=['Change 100 created.'])
        cl_num = create_changelist(
            'Solo message', 'HEAD~1', '/ws', dry_run=False)
        self.assertEqual(cl_num, '100')

    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    def test_dry_run_returns_placeholder(self, mock_get_lines):
        mock_get_lines.return_value = ['Commit']
        cl_num = create_changelist('Msg', 'HEAD~1', '/ws', dry_run=True)
        self.assertEqual(cl_num, DRY_RUN_CHANGELIST)

    @mock.patch('git_p4son.perforce.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    def test_p4_failure(self, mock_get_lines, mock_run):
        mock_get_lines.return_value = ['Commit']
        mock_run.side_effect = RunError('p4 change failed')
        with self.assertRaises(RunError):
            create_changelist('Msg', 'HEAD~1', '/ws', dry_run=False)


@mock.patch('git_p4son.perforce.run',
            return_value=make_run_result(stdout=['Change 100 created.']))
@mock.patch('git_p4son.lib.get_commit_subjects_since',
            return_value=['Add feature'])
class TestCreateChangelistWithReview(unittest.TestCase):
    def test_review_keyword_ends_the_description(self, _lines, mock_run):
        create_changelist('Title', 'HEAD~1', '/ws', review=True, dry_run=False)
        self.assertTrue(mock_run.call_args.kwargs['input'].endswith(
            '\t1. Add feature\n\t\n\t#review\n'))
        self.assertEqual(mock_run.call_count, 1)

    def test_message_with_review_keyword_gets_no_second_one(
            self, _lines, mock_run):
        create_changelist('Title\n\n#review-678', 'HEAD~1', '/ws',
                          review=True, dry_run=False)
        self.assertEqual(
            mock_run.call_args.kwargs['input'].count('#review'), 1)

    def test_keyword_in_message_moves_to_the_end(self, _lines, mock_run):
        create_changelist('Title\n\n#review-678', 'HEAD~1', '/ws',
                          review=True, dry_run=False)
        self.assertTrue(mock_run.call_args.kwargs['input'].endswith(
            '\t1. Add feature\n\t\n\t#review-678\n'))

    def test_a_mention_of_review_is_not_a_keyword(self, _lines, mock_run):
        """Only #review on a line of its own counts; #reviewer is not it."""
        create_changelist('Ask #reviewer about #review rules', 'HEAD~1', '/ws',
                          review=True, dry_run=False)
        self.assertTrue(mock_run.call_args.kwargs['input'].endswith(
            '\t\n\t#review\n'))

    def test_without_review_there_is_no_keyword(self, _lines, mock_run):
        create_changelist('Title', 'HEAD~1', '/ws', dry_run=False)
        self.assertNotIn('#review', mock_run.call_args.kwargs['input'])


class TestCreateChangelistWithoutCommitList(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    def test_leaves_out_commit_list(self, mock_get_lines, mock_run):
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 created.'])
        cl_num = create_changelist('Title\n\nBody', 'HEAD~1', '/ws',
                                   commit_list=False, dry_run=False)

        self.assertEqual(cl_num, '12345')
        mock_get_lines.assert_not_called()
        self.assertEqual(
            mock_run.call_args.kwargs['input'],
            'Change: new\n\nDescription:\n\tTitle\n\t\n\tBody\n')


class TestUpdateChangelist(unittest.TestCase):
    def _spec_input(self, mock_run):
        return mock_run.call_args.kwargs.get('input')

    @mock.patch('git_p4son.perforce.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_appends_commits_outside_the_range(self, mock_get_spec,
                                               mock_subjects, mock_run):
        """Subjects not in the old list are appended after it."""
        mock_get_spec.return_value = SAMPLE_SPEC
        mock_subjects.return_value = ['New commit A', 'New commit B']
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'HEAD~1', '/ws',
                          dry_run=False, message=None, commit_list=True)

        spec_input = self._spec_input(mock_run)
        self.assertIn('1. Add validation', spec_input)
        self.assertIn('2. Fix redirect', spec_input)
        self.assertIn('3. New commit A', spec_input)
        self.assertIn('4. New commit B', spec_input)
        self.assertIn('Fix the login bug', spec_input)

    @mock.patch('git_p4son.perforce.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_replaces_entries_covered_by_the_range(self, mock_get_spec,
                                                   mock_subjects, mock_run):
        """Old entries covered by the range are replaced, others are kept."""
        mock_get_spec.return_value = SAMPLE_SPEC
        mock_subjects.return_value = ['Fix redirect', 'New commit A']
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'HEAD~1', '/ws',
                          dry_run=False, message=None, commit_list=True)

        spec_input = self._spec_input(mock_run)
        self.assertIn('1. Add validation', spec_input)
        self.assertIn('2. Fix redirect', spec_input)
        self.assertIn('3. New commit A', spec_input)
        self.assertEqual(spec_input.count('Fix redirect'), 1)

    @mock.patch('git_p4son.perforce.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_rerunning_same_update_is_idempotent(self, mock_get_spec,
                                                 mock_subjects, mock_run):
        """Running the same update again does not duplicate the commit list."""
        mock_get_spec.return_value = SAMPLE_SPEC
        mock_subjects.return_value = ['Add validation', 'Fix redirect']
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'main', '/ws', dry_run=False,
                          message=None, commit_list=True)

        spec_input = self._spec_input(mock_run)
        self.assertIn('1. Add validation', spec_input)
        self.assertIn('2. Fix redirect', spec_input)
        self.assertNotIn('3. ', spec_input)
        self.assertEqual(spec_input.count('Add validation'), 1)
        self.assertEqual(spec_input.count('Fix redirect'), 1)

    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_dry_run(self, mock_get_spec, mock_subjects):
        mock_get_spec.return_value = SAMPLE_SPEC
        mock_subjects.return_value = ['New commit']
        update_changelist('12345', 'HEAD~1', '/ws',
                          dry_run=True, message=None, commit_list=True)

    @mock.patch('git_p4son.perforce.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_no_existing_commits_starts_at_one(self, mock_get_spec,
                                               mock_subjects, mock_run):
        mock_get_spec.return_value = SAMPLE_SPEC_NO_COMMITS
        mock_subjects.return_value = ['First commit']
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'HEAD~1', '/ws',
                          dry_run=False, message=None, commit_list=True)
        self.assertIn('1. First commit', self._spec_input(mock_run))

    @mock.patch('git_p4son.perforce.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_message_replaces_text_above_commit_list(self, mock_get_spec,
                                                     mock_subjects, mock_run):
        mock_get_spec.return_value = SAMPLE_SPEC
        mock_subjects.return_value = ['New commit']
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'HEAD~1', '/ws',
                          message='New title\n\nNew body', dry_run=False, commit_list=True)

        spec_input = self._spec_input(mock_run)
        self.assertNotIn('Fix the login bug', spec_input)
        self.assertIn(
            '\tNew title\n\t\n\tNew body\n\t\n\tChanges included:\n'
            '\t1. Add validation\n\t2. Fix redirect\n\t3. New commit\n',
            spec_input)

    @mock.patch('git_p4son.perforce.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_message_without_commit_list(self, mock_get_spec,
                                         mock_subjects, mock_run):
        """Without a list, the message replaces the whole description."""
        mock_get_spec.return_value = SAMPLE_SPEC_NO_COMMITS
        mock_subjects.return_value = []
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'HEAD~1', '/ws',
                          message='New title', dry_run=False, commit_list=True)

        spec_input = self._spec_input(mock_run)
        self.assertIn('\tNew title\n', spec_input)
        self.assertNotIn('Just a message', spec_input)
        self.assertNotIn('Changes included:', spec_input)

    @mock.patch('git_p4son.perforce.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_message_without_updating_commit_list(self, mock_get_spec,
                                                  mock_subjects, mock_run):
        mock_get_spec.return_value = SAMPLE_SPEC
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'HEAD~1', '/ws', message='New title',
                          commit_list=False, dry_run=False)

        mock_subjects.assert_not_called()
        self.assertIn(
            '\tNew title\n\t\n\tChanges included:\n'
            '\t1. Add validation\n\t2. Fix redirect\n',
            self._spec_input(mock_run))


@mock.patch('git_p4son.perforce.run')
@mock.patch('git_p4son.lib.get_commit_subjects_since')
@mock.patch('git_p4son.lib.get_changelist_spec')
class TestUpdateChangelistReviewKeyword(unittest.TestCase):
    """The Swarm review keyword stays at the end of the description."""

    def _update(self, mock_get_spec, mock_subjects, mock_run, spec,
                subjects, **kwargs):
        mock_get_spec.return_value = spec
        mock_subjects.return_value = subjects
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        kwargs = {'dry_run': False, 'message': None,
                  'commit_list': True, **kwargs}
        update_changelist('12345', 'HEAD~1', '/ws', **kwargs)
        spec_input = mock_run.call_args.kwargs['input']
        start = spec_input.index('Description:\n') + len('Description:\n')
        return spec_input[start:spec_input.index('\n\nFiles:')]

    def test_message_without_list_keeps_keyword(self, *mocks):
        description = self._update(*mocks, SPEC_NO_LIST_WITH_KEYWORD, [],
                                   message='New title', commit_list=False)
        self.assertEqual(description, '\tNew title\n\t\n\t#review-678')

    def test_list_added_above_keyword(self, *mocks):
        description = self._update(*mocks, SPEC_NO_LIST_WITH_KEYWORD,
                                   ['First commit'])
        self.assertEqual(description, (
            '\tOld title\n\t\n\tChanges included:\n\t1. First commit\n'
            '\t\n\t#review-678'))

    def test_message_with_list_keeps_keyword_once(self, *mocks):
        description = self._update(*mocks, SPEC_LIST_WITH_KEYWORD,
                                   ['Second commit'], message='New title')
        self.assertEqual(description, (
            '\tNew title\n\t\n\tChanges included:\n\t1. Add validation\n'
            '\t2. Second commit\n\t\n\t#review-678'))

    def test_keyword_in_new_message_not_duplicated(self, *mocks):
        description = self._update(*mocks, SPEC_NO_LIST_WITH_KEYWORD, [],
                                   message='New title\n\n#review-678',
                                   commit_list=False)
        self.assertEqual(description.count('#review-678'), 1)

    def test_keyword_inside_text_is_not_moved(self, *mocks):
        """Only a keyword on a line of its own is Swarm's."""
        spec = SPEC_NO_LIST_WITH_KEYWORD.replace(
            '\tOld title', '\tSee #review notes')
        description = self._update(*mocks, spec, [], commit_list=False)
        self.assertTrue(description.startswith('\tSee #review notes\n'))


class TestOpenWarnings(unittest.TestCase):
    """Files p4 did not open are found by asking again, since p4 exits 0 for most of them."""

    def _run(self, adds, opened_after, output):
        changes = LocalChanges()
        changes.adds = adds
        with mock.patch('git_p4son.lib.get_opened_files',
                        side_effect=[{}, opened_after]) as mock_opened, \
                mock.patch('git_p4son.perforce.run',
                           return_value=make_run_result(
                               returncode=1, stdout=output)), \
                captured_output() as out:
            include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        return mock_opened, out.getvalue()

    def test_warns_about_files_left_unopened(self):
        """A file matching .p4ignore is skipped with a warning and the rest still open."""
        _, out = self._run(
            ['generated.cs', 'real.txt'], {'real.txt': ('100', 'add')},
            ['//depot/real.txt#1 - opened for add',
             "generated.cs - ignored file can't be added."])
        self.assertTrue(out.endswith(
            '[warn] p4 did not open 1 of 2 files:\n'
            '  generated.cs (for add)\n'
            'p4 said:\n'
            "  generated.cs - ignored file can't be added.\n"))

    def test_file_opened_in_another_changelist_warns(self):
        _, out = self._run(['a.txt'], {'a.txt': ('200', 'add')}, [])
        self.assertIn('[warn] p4 did not open 1 of 1 file:\n  a.txt (for add)\n',
                      out)

    def test_no_warning_when_all_opened(self):
        mock_opened, out = self._run(
            ['new.txt'], {'new.txt': ('100', 'add')},
            ['//depot/new.txt#1 - opened for add'])
        self.assertNotIn('[warn]', out)
        self.assertEqual(mock_opened.call_args_list[1].args,
                         (['new.txt'], '/ws'))

    @mock.patch('git_p4son.perforce.run', return_value=make_run_result())
    @mock.patch('git_p4son.lib.get_opened_files', return_value={})
    def test_dry_run_does_not_ask_again(self, mock_opened, _run):
        changes = LocalChanges()
        changes.adds = ['new.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=True)
        mock_opened.assert_called_once()


class TestIncludeChangesInChangelist(unittest.TestCase):

    def setUp(self):
        self.mocks = {}
        for target, value in (('get_opened_files', {}),
                              ('_warn_unopened', None),
                              ('restore_files', None)):
            self.mocks[target] = start_patch(self, f'git_p4son.lib.{target}',
                                             return_value=value)
    # --- added files ---

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=None))
    @mock.patch('git_p4son.perforce.run')
    def test_adds_files(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.adds = ['new_file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'add', '-c', '100'], 'new_file.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('200', 'add')))
    @mock.patch('git_p4son.perforce.run')
    def test_reopens_added_file_in_different_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.adds = ['new_file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'reopen', '-c', '100'], 'new_file.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('100', 'add')))
    @mock.patch('git_p4son.perforce.run')
    def test_skips_added_file_already_in_correct_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.adds = ['new_file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        mock_run.assert_not_called()

    # --- modified files ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=None))
    @mock.patch('git_p4son.perforce.run')
    def test_edits_unchecked_out_file(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['mod.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'edit', '-c', '100'], 'mod.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('200', 'edit')))
    @mock.patch('git_p4son.perforce.run')
    def test_reopens_file_in_different_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['mod.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'reopen', '-c', '100'], 'mod.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('100', 'edit')))
    @mock.patch('git_p4son.perforce.run')
    def test_skips_file_already_in_correct_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['mod.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        mock_run.assert_not_called()

    # --- deleted files ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=None))
    @mock.patch('git_p4son.perforce.run')
    def test_deletes_files(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['old.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'delete', '-c', '100'], 'old.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('200', 'delete')))
    @mock.patch('git_p4son.perforce.run')
    def test_reopens_deleted_file_in_different_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['old.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'reopen', '-c', '100'], 'old.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('100', 'delete')))
    @mock.patch('git_p4son.perforce.run')
    def test_skips_deleted_file_already_in_correct_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['old.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        mock_run.assert_not_called()

    # --- moved files ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=None))
    @mock.patch('git_p4son.perforce.run')
    def test_moves_files(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.moves = [('old.txt', 'new.txt')]
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'add', '-c', '100'], 'new.txt'),
            (['p4', '-L', 'en', '-x', '-', 'delete', '-c', '100'], 'old.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(side_effect=[('200', 'delete'), ('200', 'add')]))
    @mock.patch('git_p4son.perforce.run')
    def test_reopens_moved_files_in_different_changelist(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.moves = [('old.txt', 'new.txt')]
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'reopen', '-c', '100'], 'old.txt\nnew.txt')])

    # --- dry run ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=None))
    @mock.patch('git_p4son.perforce.run')
    def test_dry_run(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.adds = ['new.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=True)
        mock_run.assert_called_once_with(
            ['p4', '-L', 'en', '-x', '-', 'add', '-c', '100'], cwd='/ws',
            input='new.txt', dry_run=True, fail_on_returncode=False)


class TestOneOpenedQuery(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run', return_value=make_run_result())
    @mock.patch('git_p4son.lib.get_opened_files', return_value={})
    def test_every_path_in_one_query(self, mock_opened, _run):
        changes = LocalChanges()
        changes.adds = ['new.txt']
        changes.mods = ['mod.txt']
        changes.dels = ['gone.txt']
        changes.moves = [('old.txt', 'renamed.txt')]
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(
            mock_opened.call_args_list[0].args,
            (['new.txt', 'mod.txt', 'gone.txt', 'old.txt', 'renamed.txt'], '/ws'))


class TestActionMismatch(unittest.TestCase):
    """Tests for reopening files when the p4 action doesn't match the desired action."""

    def setUp(self):
        self.mocks = {}
        for target, value in (('get_opened_files', {}),
                              ('_warn_unopened', None),
                              ('restore_files', None)):
            self.mocks[target] = start_patch(self, f'git_p4son.lib.{target}',
                                             return_value=value)

    # --- edit -> delete ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('100', 'edit')))
    @mock.patch('git_p4son.perforce.run')
    def test_edit_to_delete_same_cl(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'revert'], 'file.txt'),
            (['p4', '-L', 'en', '-x', '-', 'delete', '-c', '100'], 'file.txt')])

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('200', 'edit')))
    @mock.patch('git_p4son.perforce.run')
    def test_edit_to_delete_different_cl(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'revert'], 'file.txt'),
            (['p4', '-L', 'en', '-x', '-', 'delete', '-c', '100'], 'file.txt')])

    # --- delete -> edit ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('100', 'delete')))
    @mock.patch('git_p4son.perforce.run')
    def test_delete_to_edit_same_cl(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'revert'], 'file.txt'),
            (['p4', '-L', 'en', '-x', '-', 'edit', '-c', '100'], 'file.txt')])
        self.mocks['restore_files'].assert_called_once_with(
            ['file.txt'], '/ws', False)

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('200', 'delete')))
    @mock.patch('git_p4son.perforce.run')
    def test_delete_to_edit_different_cl(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'revert'], 'file.txt'),
            (['p4', '-L', 'en', '-x', '-', 'edit', '-c', '100'], 'file.txt')])
        self.mocks['restore_files'].assert_called_once_with(
            ['file.txt'], '/ws', False)

    # --- add -> edit (keep as add, file is new to depot) ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('100', 'add')))
    @mock.patch('git_p4son.perforce.run')
    def test_add_to_edit_same_cl_keeps_add(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        mock_run.assert_not_called()

    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('200', 'add')))
    @mock.patch('git_p4son.perforce.run')
    def test_add_to_edit_different_cl_reopens(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.mods = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'reopen', '-c', '100'], 'file.txt')])

    # --- add -> delete (revert only, no reopen) ---
    @mock.patch('git_p4son.lib.opened_lookup',
                return_value=mock.Mock(return_value=('100', 'add')))
    @mock.patch('git_p4son.perforce.run')
    def test_add_to_delete_reverts_only(self, mock_run, mock_check):
        mock_run.return_value = make_run_result()
        changes = LocalChanges()
        changes.dels = ['file.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
                         (['p4', '-L', 'en', '-x', '-', 'revert'], 'file.txt')])


class TestBatchedActions(unittest.TestCase):
    @mock.patch('git_p4son.lib.restore_files')
    @mock.patch('git_p4son.lib._warn_unopened')
    @mock.patch('git_p4son.perforce.run', return_value=make_run_result())
    @mock.patch('git_p4son.lib.get_opened_files', return_value={
        'a.txt': ('100', 'delete'), 'b.txt': ('200', 'delete'),
        'c.txt': ('200', 'edit'), 'd.txt': ('300', 'edit')})
    def test_reverts_reopens_and_restores_in_one_call_each(
            self, _opened, mock_run, _warn, mock_restore):
        changes = LocalChanges()
        changes.mods = ['a.txt', 'b.txt', 'c.txt', 'd.txt']
        with captured_output() as out:
            include_changes_in_changelist(changes, '100', '/ws', dry_run=False)
        self.assertEqual(_commands(mock_run), [
            (['p4', '-L', 'en', '-x', '-', 'revert'], 'a.txt\nb.txt'),
            (['p4', '-L', 'en', '-x', '-', 'edit', '-c', '100'], 'a.txt\nb.txt'),
            (['p4', '-L', 'en', '-x', '-', 'reopen', '-c', '100'], 'c.txt\nd.txt'),
        ])
        mock_restore.assert_called_once_with(['a.txt', 'b.txt'], '/ws', False)
        self.assertIn('Moving files from other changelists:\n  c.txt\n  d.txt\n',
                      out.getvalue())

    @mock.patch('git_p4son.perforce.run', return_value=make_run_result())
    @mock.patch('git_p4son.lib.get_opened_files',
                return_value={'a.txt': ('200', 'delete')})
    def test_dry_run_reaches_every_batch(self, _opened, mock_run):
        changes = LocalChanges()
        changes.mods = ['a.txt']
        include_changes_in_changelist(changes, '100', '/ws', dry_run=True)
        self.assertTrue(all(c.kwargs['dry_run']
                            for c in mock_run.call_args_list))


class TestKeepWorkingFiles(WorkspaceTestCase):
    """A file p4 revert overwrites gets git's version back, or with keep_working_files its working version."""

    def setUp(self):
        super().setUp()
        self.write_file('a.txt', 'working')
        self.restore = start_patch(self, 'git_p4son.lib.restore_files')
        start_patch(self, 'git_p4son.lib._warn_unopened')
        start_patch(self, 'git_p4son.lib.get_opened_files',
                    return_value={'a.txt': ('100', 'delete')})
        start_patch(self, 'git_p4son.perforce.run', side_effect=self._p4)

    def _p4(self, command, dry_run=False, **kwargs):
        if 'revert' in command and not dry_run:
            path = os.path.join(self.ws, 'a.txt')
            with open(path, 'w') as f:
                f.write('depot')
            os.chmod(path, stat.S_IRUSR)
        return make_run_result()

    def _include(self, keep, dry_run=False):
        changes = LocalChanges()
        changes.mods = ['a.txt']
        with captured_output() as out:
            include_changes_in_changelist(changes, '100', self.ws, dry_run,
                                          keep_working_files=keep)
        return out.getvalue()

    def _read(self):
        with open(os.path.join(self.ws, 'a.txt')) as f:
            return f.read()

    def test_restores_git_version(self):
        self._include(keep=False)
        self.restore.assert_called_once_with(['a.txt'], self.ws, False)

    def test_keeps_the_working_file(self):
        self._include(keep=True)
        self.restore.assert_not_called()
        self.assertEqual(self._read(), 'working')

    def test_dry_run_says_what_it_would_put_back(self):
        out = self._include(keep=True, dry_run=True)
        self.assertIn('Would put back 1 working file', out)
        self.assertEqual(self._read(), 'working')


class TestRevertUnchangedFiles(WorkspaceTestCase):
    """The tracked/untracked boundary decides what may be reverted."""

    def setUp(self):
        super().setUp()
        patches = {
            'opened': mock.patch(
                'git_p4son.lib.get_opened_files_in_changelist'),
            'tracked': mock.patch('git_p4son.lib.get_tracked_files'),
            'revert_unchanged': mock.patch(
                'git_p4son.lib.p4_revert_unchanged', return_value=[]),
            'revert': mock.patch('git_p4son.lib.p4_revert'),
            'restore': mock.patch('git_p4son.lib.restore_files'),
            'writable_mode': mock.patch(
                'git_p4son.lib.is_writable_mode', return_value=False),
            'make_writable': mock.patch('git_p4son.writable.make_writable'),
        }
        self.mocks = {name: p.start() for name, p in patches.items()}
        self.addCleanup(mock.patch.stopall)
        self.out = self.enterContext(captured_output())

    def _setup(self, opened, tracked, on_disk):
        self.mocks['opened'].return_value = opened
        self.mocks['tracked'].return_value = set(tracked)
        for path in on_disk:
            with open(os.path.join(self.ws, path), 'w') as f:
                f.write('content')

    def _unchanged_candidates(self):
        return self.mocks['revert_unchanged'].call_args.args[0]

    def test_tracked_edit_is_revert_candidate(self):
        self._setup([('a.txt', 'edit')], ['a.txt'], ['a.txt'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.assertEqual(self._unchanged_candidates(), ['a.txt'])
        self.mocks['revert_unchanged'].assert_called_once_with(
            ['a.txt'], '100', self.ws, False)

    def test_untracked_edit_is_never_reverted(self):
        """A p4-only file checked out by hand stays opened, even when unchanged."""
        self._setup([('Asset.uasset', 'edit')], [], ['Asset.uasset'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.assertEqual(self._unchanged_candidates(), [])
        self.mocks['revert'].assert_not_called()

    def test_tracked_deletes_present_on_disk_are_reverted_and_restored(self):
        self._setup([('a.txt', 'delete'), ('b.txt', 'delete')],
                    ['a.txt', 'b.txt'], ['a.txt', 'b.txt'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.mocks['revert'].assert_called_once_with(
            ['a.txt', 'b.txt'], self.ws, False)
        self.mocks['restore'].assert_called_once_with(
            ['a.txt', 'b.txt'], self.ws, False)
        self.assertTrue(self.out.getvalue().endswith(
            '[ok] 2 files reverted\n'))

    def test_real_delete_is_left_alone(self):
        """Opened for delete and gone from git and disk: a real delete."""
        self._setup([('gone.txt', 'delete')], [], [])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.mocks['revert'].assert_not_called()
        self.assertEqual(self._unchanged_candidates(), [])

    def test_missing_add_is_revert_candidate_tracked_or_not(self):
        self._setup([('was_added.txt', 'add'), ('tracked_add.txt', 'add')],
                    ['tracked_add.txt'], [])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.assertEqual(self._unchanged_candidates(),
                         ['was_added.txt', 'tracked_add.txt'])

    def test_untracked_add_present_on_disk_is_left_alone(self):
        self._setup([('New.uasset', 'add')], [], ['New.uasset'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.assertEqual(self._unchanged_candidates(), [])
        self.mocks['revert'].assert_not_called()

    def test_tracked_add_present_on_disk_is_left_alone(self):
        """A real new file stays opened; there is no depot version to compare with."""
        self._setup([('new.txt', 'add')], ['new.txt'], ['new.txt'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.assertEqual(self._unchanged_candidates(), [])

    def test_other_actions_are_left_alone(self):
        self._setup([('m.txt', 'move/add'), ('d.txt', 'move/delete'),
                     ('i.txt', 'integrate'), ('b.txt', 'branch')],
                    ['m.txt', 'i.txt', 'b.txt'],
                    ['m.txt', 'i.txt', 'b.txt'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.assertEqual(self._unchanged_candidates(), [])
        self.mocks['revert'].assert_not_called()

    def test_counts_reverted_files(self):
        self._setup([('a.txt', 'edit'), ('b.txt', 'edit'),
                     ('c.txt', 'delete')],
                    ['a.txt', 'b.txt', 'c.txt'], ['a.txt', 'b.txt', 'c.txt'])
        self.mocks['revert_unchanged'].return_value = [
            '//depot/a.txt#1 - was edit, reverted']
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.assertTrue(self.out.getvalue().endswith(
            '[ok] 2 files reverted\n'))

    def test_dry_run_reverts_and_restores_nothing(self):
        self._setup([('a.txt', 'edit'), ('c.txt', 'delete')],
                    ['a.txt', 'c.txt'], ['a.txt', 'c.txt'])
        revert_unchanged_files('100', self.ws, dry_run=True)
        self.mocks['revert_unchanged'].assert_called_once_with(
            ['a.txt'], '100', self.ws, True)
        self.mocks['revert'].assert_called_once_with(['c.txt'], self.ws, True)

    def test_dry_run_placeholder_changelist_queries_nothing(self):
        revert_unchanged_files(DRY_RUN_CHANGELIST, self.ws, dry_run=True)
        self.mocks['opened'].assert_not_called()
        self.mocks['revert_unchanged'].assert_not_called()

    def test_writable_mode_makes_tracked_files_writable(self):
        self.mocks['writable_mode'].return_value = True
        self._setup([('a.txt', 'edit'), ('c.txt', 'delete'),
                     ('gone.txt', 'add')],
                    ['a.txt', 'c.txt'], ['a.txt', 'c.txt'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.mocks['make_writable'].assert_called_once_with([
            os.path.join(self.ws, 'a.txt'),
            os.path.join(self.ws, 'c.txt'),
        ])

    def test_revision_keeps_the_working_files(self):
        self._setup([('c.txt', 'delete')], ['c.txt'], ['c.txt'])
        self.mocks['revert'].side_effect = lambda files, ws, dry_run: (
            open(os.path.join(ws, 'c.txt'), 'w').write('depot'))
        revert_unchanged_files('100', self.ws, dry_run=False, revision='abc')
        self.mocks['tracked'].assert_called_once_with(
            ['c.txt'], self.ws, 'abc')
        self.mocks['restore'].assert_not_called()
        with open(os.path.join(self.ws, 'c.txt')) as f:
            self.assertEqual(f.read(), 'content')

    def test_writable_mode_off_leaves_permissions(self):
        self._setup([('a.txt', 'edit')], ['a.txt'], ['a.txt'])
        revert_unchanged_files('100', self.ws, dry_run=False)
        self.mocks['make_writable'].assert_not_called()


if __name__ == '__main__':
    unittest.main()
