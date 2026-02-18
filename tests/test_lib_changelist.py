"""Tests for changelist functions in git_p4son.lib module."""

import unittest
from unittest import mock

from git_p4son.common import CommandError
from git_p4son.lib import (
    create_changelist,
    extract_description_lines,
    get_changelist_spec,
    replace_description_in_spec,
    split_description_lines,
    update_changelist,
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
    def test_dry_run(self, mock_get_lines):
        mock_get_lines.return_value = ['1. Commit']
        cl_num = create_changelist('Msg', 'HEAD~1', '/ws', dry_run=True)
        self.assertIsNone(cl_num)

    @mock.patch('git_p4son.lib.run')
    @mock.patch('git_p4son.lib.get_enumerated_commit_lines_since')
    def test_p4_failure(self, mock_get_lines, mock_run):
        mock_get_lines.return_value = ['1. Commit']
        mock_run.side_effect = CommandError('p4 change failed')
        with self.assertRaises(CommandError):
            create_changelist('Msg', 'HEAD~1', '/ws')


class TestGetChangelistSpec(unittest.TestCase):
    @mock.patch('git_p4son.lib.run')
    def test_success(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=SAMPLE_SPEC.splitlines())
        spec = get_changelist_spec('12345', '/ws')
        self.assertEqual(spec, SAMPLE_SPEC)

    @mock.patch('git_p4son.lib.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = CommandError('Changelist not found')
        with self.assertRaises(CommandError):
            get_changelist_spec('99999', '/ws')


class TestUpdateChangelist(unittest.TestCase):
    @mock.patch('git_p4son.lib.run')
    @mock.patch('git_p4son.lib.get_enumerated_commit_lines_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_appends_new_commits(self, mock_get_spec, mock_get_lines, mock_run):
        mock_get_spec.return_value = SAMPLE_SPEC
        # New commits should be numbered 3 and 4 (continuing from existing 1, 2)
        mock_get_lines.return_value = ['3. New commit A', '4. New commit B']
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'HEAD~1', '/ws')
        # Verify start_number was passed correctly (2 existing commits -> start at 3)
        mock_get_lines.assert_called_once_with('HEAD~1', '/ws', start_number=3)
        # Verify the new spec was passed
        call_kwargs = mock_run.call_args
        spec_input = call_kwargs.kwargs.get('input')
        # Old commits preserved
        self.assertIn('1. Add validation', spec_input)
        self.assertIn('2. Fix redirect', spec_input)
        # New commits appended
        self.assertIn('3. New commit A', spec_input)
        self.assertIn('4. New commit B', spec_input)
        # user message preserved
        self.assertIn('Fix the login bug', spec_input)

    @mock.patch('git_p4son.lib.get_enumerated_commit_lines_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_dry_run(self, mock_get_spec, mock_get_lines):
        mock_get_spec.return_value = SAMPLE_SPEC
        mock_get_lines.return_value = ['3. New commit']
        update_changelist('12345', 'HEAD~1', '/ws', dry_run=True)

    @mock.patch('git_p4son.lib.run')
    @mock.patch('git_p4son.lib.get_enumerated_commit_lines_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_no_existing_commits_starts_at_one(self, mock_get_spec, mock_get_lines, mock_run):
        mock_get_spec.return_value = SAMPLE_SPEC_NO_COMMITS
        mock_get_lines.return_value = ['1. First commit']
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 updated.'])
        update_changelist('12345', 'HEAD~1', '/ws')
        # Should start at 1 since no existing commits
        mock_get_lines.assert_called_once_with('HEAD~1', '/ws', start_number=1)


if __name__ == '__main__':
    unittest.main()
