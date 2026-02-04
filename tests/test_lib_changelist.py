"""Tests for changelist functions in git_p4son.lib module."""

import unittest
from unittest import mock

from git_p4son.lib import (
    create_changelist,
    extract_description,
    get_changelist_spec,
    replace_description_in_spec,
    split_description_message_and_commits,
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


class TestExtractDescription(unittest.TestCase):
    def test_extracts_multiline_description(self):
        desc = extract_description(SAMPLE_SPEC)
        self.assertEqual(
            desc, 'Fix the login bug\n1. Add validation\n2. Fix redirect')

    def test_extracts_simple_description(self):
        desc = extract_description(SAMPLE_SPEC_NO_COMMITS)
        self.assertEqual(desc, 'Just a message')

    def test_empty_spec(self):
        desc = extract_description('')
        self.assertEqual(desc, '')


class TestReplaceDescriptionInSpec(unittest.TestCase):
    def test_replaces_description(self):
        new_spec = replace_description_in_spec(
            SAMPLE_SPEC, 'New description\nLine 2')
        self.assertIn('\tNew description\n', new_spec)
        self.assertIn('\tLine 2\n', new_spec)
        # Old description should be gone
        self.assertNotIn('Fix the login bug', new_spec)

    def test_preserves_other_fields(self):
        new_spec = replace_description_in_spec(SAMPLE_SPEC, 'Replaced')
        self.assertIn('Change:\t12345', new_spec)
        self.assertIn('Files:', new_spec)


class TestSplitDescriptionMessageAndCommits(unittest.TestCase):
    def test_splits_message_and_commits(self):
        desc = 'Fix the login bug\n1. Add validation\n2. Fix redirect'
        msg, commits, trailing = split_description_message_and_commits(desc)
        self.assertEqual(msg, 'Fix the login bug')
        self.assertEqual(commits, '1. Add validation\n2. Fix redirect')
        self.assertEqual(trailing, '')

    def test_no_commits(self):
        desc = 'Just a message'
        msg, commits, trailing = split_description_message_and_commits(desc)
        self.assertEqual(msg, 'Just a message')
        self.assertEqual(commits, '')
        self.assertEqual(trailing, '')

    def test_trailing_text_after_commits(self):
        desc = 'Message\n1. First\n2. Second\nTrailing note'
        msg, commits, trailing = split_description_message_and_commits(desc)
        self.assertEqual(msg, 'Message')
        self.assertEqual(commits, '1. First\n2. Second')
        self.assertEqual(trailing, 'Trailing note')

    def test_empty_description(self):
        msg, commits, trailing = split_description_message_and_commits('')
        self.assertEqual(msg, '')
        self.assertEqual(commits, '')
        self.assertEqual(trailing, '')

    def test_multiline_message_before_commits(self):
        desc = 'Line 1\nLine 2\n1. Commit one'
        msg, commits, trailing = split_description_message_and_commits(desc)
        self.assertEqual(msg, 'Line 1\nLine 2')
        self.assertEqual(commits, '1. Commit one')
        self.assertEqual(trailing, '')


class TestCreateChangelist(unittest.TestCase):
    @mock.patch('git_p4son.lib.subprocess.run')
    @mock.patch('git_p4son.lib.get_enumerated_change_description_since')
    def test_creates_changelist(self, mock_get_desc, mock_subprocess):
        mock_get_desc.return_value = (0, '1. Add feature\n2. Fix bug')
        mock_subprocess.return_value = mock.Mock(
            returncode=0,
            stdout='Change 99999 created.\n',
            stderr='',
        )
        rc, cl_num = create_changelist('My message', 'HEAD~1', '/ws')
        self.assertEqual(rc, 0)
        self.assertEqual(cl_num, '99999')
        # Verify spec was passed via stdin
        call_kwargs = mock_subprocess.call_args
        spec_input = call_kwargs.kwargs.get(
            'input') or call_kwargs[1].get('input')
        self.assertIn('My message', spec_input)
        self.assertIn('1. Add feature', spec_input)

    @mock.patch('git_p4son.lib.subprocess.run')
    @mock.patch('git_p4son.lib.get_enumerated_change_description_since')
    def test_no_commits(self, mock_get_desc, mock_subprocess):
        mock_get_desc.return_value = (0, None)
        mock_subprocess.return_value = mock.Mock(
            returncode=0,
            stdout='Change 100 created.\n',
            stderr='',
        )
        rc, cl_num = create_changelist('Solo message', 'HEAD~1', '/ws')
        self.assertEqual(rc, 0)
        self.assertEqual(cl_num, '100')

    @mock.patch('git_p4son.lib.get_enumerated_change_description_since')
    def test_dry_run(self, mock_get_desc):
        mock_get_desc.return_value = (0, '1. Commit')
        rc, cl_num = create_changelist('Msg', 'HEAD~1', '/ws', dry_run=True)
        self.assertEqual(rc, 0)
        self.assertIsNone(cl_num)

    @mock.patch('git_p4son.lib.subprocess.run')
    @mock.patch('git_p4son.lib.get_enumerated_change_description_since')
    def test_p4_failure(self, mock_get_desc, mock_subprocess):
        mock_get_desc.return_value = (0, '1. Commit')
        mock_subprocess.return_value = mock.Mock(
            returncode=1,
            stdout='',
            stderr='Error creating changelist',
        )
        rc, cl_num = create_changelist('Msg', 'HEAD~1', '/ws')
        self.assertEqual(rc, 1)
        self.assertIsNone(cl_num)


class TestGetChangelistSpec(unittest.TestCase):
    @mock.patch('git_p4son.lib.subprocess.run')
    def test_success(self, mock_subprocess):
        mock_subprocess.return_value = mock.Mock(
            returncode=0,
            stdout=SAMPLE_SPEC,
            stderr='',
        )
        rc, spec = get_changelist_spec('12345', '/ws')
        self.assertEqual(rc, 0)
        self.assertEqual(spec, SAMPLE_SPEC)

    @mock.patch('git_p4son.lib.subprocess.run')
    def test_failure(self, mock_subprocess):
        mock_subprocess.return_value = mock.Mock(
            returncode=1,
            stdout='',
            stderr='Changelist not found',
        )
        rc, spec = get_changelist_spec('99999', '/ws')
        self.assertEqual(rc, 1)
        self.assertIsNone(spec)


class TestUpdateChangelist(unittest.TestCase):
    @mock.patch('git_p4son.lib.subprocess.run')
    @mock.patch('git_p4son.lib.get_enumerated_change_description_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_updates_commit_list(self, mock_get_spec, mock_get_desc, mock_subprocess):
        mock_get_spec.return_value = (0, SAMPLE_SPEC)
        mock_get_desc.return_value = (0, '1. New commit A\n2. New commit B')
        mock_subprocess.return_value = mock.Mock(
            returncode=0,
            stdout='Change 12345 updated.',
            stderr='',
        )
        rc = update_changelist('12345', 'HEAD~1', '/ws')
        self.assertEqual(rc, 0)
        # Verify the new spec was passed
        call_kwargs = mock_subprocess.call_args
        spec_input = call_kwargs.kwargs.get(
            'input') or call_kwargs[1].get('input')
        self.assertIn('New commit A', spec_input)
        # user message preserved
        self.assertIn('Fix the login bug', spec_input)

    @mock.patch('git_p4son.lib.get_enumerated_change_description_since')
    @mock.patch('git_p4son.lib.get_changelist_spec')
    def test_dry_run(self, mock_get_spec, mock_get_desc):
        mock_get_spec.return_value = (0, SAMPLE_SPEC)
        mock_get_desc.return_value = (0, '1. New commit')
        rc = update_changelist('12345', 'HEAD~1', '/ws', dry_run=True)
        self.assertEqual(rc, 0)


if __name__ == '__main__':
    unittest.main()
