"""Tests for changelist functions in git_p4son.lib and git_p4son.perforce modules."""

import unittest
from unittest import mock

from git_p4son.common import CommandError, RunError
from git_p4son.lib import (
    create_changelist,
    split_description_lines,
    update_changelist,
)
from git_p4son.perforce import (
    extract_description_lines,
    get_changelist_spec,
    replace_description_in_spec,
)
from tests.helpers import make_run_result

SAMPLE_SPEC = """\
# A Perforce Change Specification.
Change:\t12345

Client:\tmyclient

User:\tmyuser

Status:\tpending

Description:
\tFix the login bug
\t1. Add validation
\t2. Fix redirect

Files:
\t//depot/src/login.py\t# edit
"""

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

SAMPLE_SPEC_NO_COMMITS = """\
Change:\tnew

Description:
\tJust a message

Files:
"""


class TestExtractDescriptionLines(unittest.TestCase):
    def test_extracts_multiline_description(self):
        lines = extract_description_lines(SAMPLE_SPEC)
        self.assertEqual(
            lines, ['Fix the login bug', '1. Add validation', '2. Fix redirect'])

    def test_extracts_simple_description(self):
        lines = extract_description_lines(SAMPLE_SPEC_NO_COMMITS)
        self.assertEqual(lines, ['Just a message'])

    def test_empty_spec(self):
        lines = extract_description_lines('')
        self.assertEqual(lines, [])


class TestReplaceDescriptionInSpec(unittest.TestCase):
    def test_replaces_description(self):
        new_spec = replace_description_in_spec(
            SAMPLE_SPEC, ['New description', 'Line 2'])
        self.assertIn('\tNew description\n', new_spec)
        self.assertIn('\tLine 2\n', new_spec)
        # Old description should be gone
        self.assertNotIn('Fix the login bug', new_spec)

    def test_preserves_other_fields(self):
        new_spec = replace_description_in_spec(SAMPLE_SPEC, ['Replaced'])
        self.assertIn('Change:\t12345', new_spec)
        self.assertIn('Files:', new_spec)


class TestSplitDescriptionLines(unittest.TestCase):
    def test_splits_message_and_commits(self):
        lines = ['Fix the login bug', '1. Add validation', '2. Fix redirect']
        msg, commits, trailing = split_description_lines(lines)
        self.assertEqual(msg, ['Fix the login bug'])
        self.assertEqual(commits, ['1. Add validation', '2. Fix redirect'])
        self.assertEqual(trailing, [])

    def test_no_commits(self):
        lines = ['Just a message']
        msg, commits, trailing = split_description_lines(lines)
        self.assertEqual(msg, ['Just a message'])
        self.assertEqual(commits, [])
        self.assertEqual(trailing, [])

    def test_trailing_text_after_commits(self):
        lines = ['Message', '1. First', '2. Second', 'Trailing note']
        msg, commits, trailing = split_description_lines(lines)
        self.assertEqual(msg, ['Message'])
        self.assertEqual(commits, ['1. First', '2. Second'])
        self.assertEqual(trailing, ['Trailing note'])

    def test_empty_description(self):
        msg, commits, trailing = split_description_lines([])
        self.assertEqual(msg, [])
        self.assertEqual(commits, [])
        self.assertEqual(trailing, [])

    def test_multiline_message_before_commits(self):
        lines = ['Line 1', 'Line 2', '1. Commit one']
        msg, commits, trailing = split_description_lines(lines)
        self.assertEqual(msg, ['Line 1', 'Line 2'])
        self.assertEqual(commits, ['1. Commit one'])
        self.assertEqual(trailing, [])

    def test_numbered_list_in_user_message_not_mistaken_for_commits(self):
        """A numbered list inside the user's own message must stay in the
        message; the commit list is anchored on the marker heading."""
        lines = [
            'Reasons for this change:',
            '1. performance',
            '2. simplicity',
            '',
            'Changes included:',
            '1. Add cache',
            '2. Remove old path',
        ]
        msg, commits, trailing = split_description_lines(lines)
        self.assertEqual(msg, ['Reasons for this change:',
                               '1. performance',
                               '2. simplicity',
                               '',
                               'Changes included:'])
        self.assertEqual(commits, ['1. Add cache', '2. Remove old path'])
        self.assertEqual(trailing, [])


class TestCreateChangelist(unittest.TestCase):
    @mock.patch('git_p4son.lib.run')
    @mock.patch('git_p4son.lib.get_enumerated_commit_lines_since')
    def test_creates_changelist(self, mock_get_lines, mock_run):
        mock_get_lines.return_value = ['1. Add feature', '2. Fix bug']
        mock_run.return_value = make_run_result(
            stdout=['Change 99999 created.'])
        cl_num = create_changelist('My message', 'HEAD~1', '/ws')
        self.assertEqual(cl_num, '99999')
        # Verify spec was passed via stdin
        call_kwargs = mock_run.call_args
        spec_input = call_kwargs.kwargs.get('input')
        self.assertIn('My message', spec_input)
        self.assertIn('1. Add feature', spec_input)

    @mock.patch('git_p4son.lib.run')
    @mock.patch('git_p4son.lib.get_enumerated_commit_lines_since')
    def test_no_commits(self, mock_get_lines, mock_run):
        mock_get_lines.return_value = []
        mock_run.return_value = make_run_result(
            stdout=['Change 100 created.'])
        cl_num = create_changelist('Solo message', 'HEAD~1', '/ws')
        self.assertEqual(cl_num, '100')

    @mock.patch('git_p4son.lib.get_enumerated_commit_lines_since')
    def test_dry_run_returns_placeholder(self, mock_get_lines):
        """Dry run returns a placeholder usable in downstream commands."""
        mock_get_lines.return_value = ['1. Commit']
        cl_num = create_changelist('Msg', 'HEAD~1', '/ws', dry_run=True)
        self.assertEqual(cl_num, '<changelist>')

    @mock.patch('git_p4son.lib.run')
    @mock.patch('git_p4son.lib.get_enumerated_commit_lines_since')
    def test_p4_failure(self, mock_get_lines, mock_run):
        mock_get_lines.return_value = ['1. Commit']
        mock_run.side_effect = RunError('p4 change failed')
        with self.assertRaises(RunError):
            create_changelist('Msg', 'HEAD~1', '/ws')


class TestCreateChangelistWithoutCommitList(unittest.TestCase):
    @mock.patch('git_p4son.lib.run')
    @mock.patch('git_p4son.lib.get_enumerated_commit_lines_since')
    def test_leaves_out_commit_list(self, mock_get_lines, mock_run):
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 created.'])
        cl_num = create_changelist('Title\n\nBody', 'HEAD~1', '/ws',
                                   commit_list=False)

        self.assertEqual(cl_num, '12345')
        mock_get_lines.assert_not_called()
        self.assertEqual(
            mock_run.call_args.kwargs['input'],
            'Change: new\n\nDescription:\n\tTitle\n\t\n\tBody\n')


class TestGetChangelistSpec(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_success(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=SAMPLE_SPEC.splitlines())
        spec = get_changelist_spec('12345', '/ws')
        self.assertEqual(spec, SAMPLE_SPEC)

    @mock.patch('git_p4son.perforce.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = RunError('Changelist not found')
        with self.assertRaises(RunError):
            get_changelist_spec('99999', '/ws')


class TestUpdateChangelist(unittest.TestCase):
    def _spec_input(self, mock_run):
        return mock_run.call_args.kwargs.get('input')

    @mock.patch('git_p4son.lib.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_appends_commits_outside_the_range(self, mock_get_spec,
                                               mock_subjects, mock_run):
        """Subjects not in the old list append after it (the review rebase
        flow updates with -b HEAD~1 per picked commit)."""
        mock_get_spec.return_value = SAMPLE_SPEC
        mock_subjects.return_value = ['New commit A', 'New commit B']
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'HEAD~1', '/ws')

        spec_input = self._spec_input(mock_run)
        self.assertIn('1. Add validation', spec_input)
        self.assertIn('2. Fix redirect', spec_input)
        self.assertIn('3. New commit A', spec_input)
        self.assertIn('4. New commit B', spec_input)
        # user message preserved
        self.assertIn('Fix the login bug', spec_input)

    @mock.patch('git_p4son.lib.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_replaces_entries_covered_by_the_range(self, mock_get_spec,
                                                   mock_subjects, mock_run):
        """An old entry whose subject is in the range is replaced, not
        duplicated; entries outside the range are kept."""
        mock_get_spec.return_value = SAMPLE_SPEC
        mock_subjects.return_value = ['Fix redirect', 'New commit A']
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'HEAD~1', '/ws')

        spec_input = self._spec_input(mock_run)
        self.assertIn('1. Add validation', spec_input)
        self.assertIn('2. Fix redirect', spec_input)
        self.assertIn('3. New commit A', spec_input)
        self.assertEqual(spec_input.count('Fix redirect'), 1)

    @mock.patch('git_p4son.lib.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_rerunning_same_update_is_idempotent(self, mock_get_spec,
                                                 mock_subjects, mock_run):
        """Running update again with the same range must not duplicate
        the commit list."""
        mock_get_spec.return_value = SAMPLE_SPEC
        mock_subjects.return_value = ['Add validation', 'Fix redirect']
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'main', '/ws')

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
        update_changelist('12345', 'HEAD~1', '/ws', dry_run=True)

    @mock.patch('git_p4son.lib.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_no_existing_commits_starts_at_one(self, mock_get_spec,
                                               mock_subjects, mock_run):
        mock_get_spec.return_value = SAMPLE_SPEC_NO_COMMITS
        mock_subjects.return_value = ['First commit']
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'HEAD~1', '/ws')
        self.assertIn('1. First commit', self._spec_input(mock_run))

    @mock.patch('git_p4son.lib.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_message_replaces_text_above_commit_list(self, mock_get_spec,
                                                     mock_subjects, mock_run):
        mock_get_spec.return_value = SAMPLE_SPEC
        mock_subjects.return_value = ['New commit']
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'HEAD~1', '/ws',
                          message='New title\n\nNew body')

        spec_input = self._spec_input(mock_run)
        self.assertNotIn('Fix the login bug', spec_input)
        self.assertIn(
            '\tNew title\n\t\n\tNew body\n\t\n\tChanges included:\n'
            '\t1. Add validation\n\t2. Fix redirect\n\t3. New commit\n',
            spec_input)

    @mock.patch('git_p4son.lib.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_message_without_commit_list(self, mock_get_spec,
                                         mock_subjects, mock_run):
        """Without a list, the message replaces the whole description."""
        mock_get_spec.return_value = SAMPLE_SPEC_NO_COMMITS
        mock_subjects.return_value = []
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'HEAD~1', '/ws', message='New title')

        spec_input = self._spec_input(mock_run)
        self.assertIn('\tNew title\n', spec_input)
        self.assertNotIn('Just a message', spec_input)
        self.assertNotIn('Changes included:', spec_input)

    @mock.patch('git_p4son.lib.run')
    @mock.patch('git_p4son.lib.get_commit_subjects_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_message_without_updating_commit_list(self, mock_get_spec,
                                                  mock_subjects, mock_run):
        mock_get_spec.return_value = SAMPLE_SPEC
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'HEAD~1', '/ws', message='New title',
                          commit_list=False)

        mock_subjects.assert_not_called()
        self.assertIn(
            '\tNew title\n\t\n\tChanges included:\n'
            '\t1. Add validation\n\t2. Fix redirect\n',
            self._spec_input(mock_run))


@mock.patch('git_p4son.lib.run')
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


if __name__ == '__main__':
    unittest.main()
