"""Tests for review/shelve functions in git_p4son.lib module."""

import unittest
from unittest import mock

from git_p4son.common import CommandError
from git_p4son.lib import (
    p4_shelve_changelist,
    add_review_keyword_to_changelist,
)
from tests.helpers import make_run_result


class TestP4ShelveChangelist(unittest.TestCase):
    @mock.patch('git_p4son.lib.run')
    def test_success(self, mock_run):
        mock_run.return_value = make_run_result()
        p4_shelve_changelist('100', '/ws')
        mock_run.assert_called_once_with(
            ['p4', 'shelve', '-f', '-Af', '-c', '100'],
            cwd='/ws', dry_run=False,
        )

    @mock.patch('git_p4son.lib.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = CommandError('shelve failed')
        with self.assertRaises(CommandError):
            p4_shelve_changelist('100', '/ws')

    @mock.patch('git_p4son.lib.run')
    def test_dry_run(self, mock_run):
        mock_run.return_value = make_run_result()
        p4_shelve_changelist('100', '/ws', dry_run=True)
        mock_run.assert_called_once_with(
            ['p4', 'shelve', '-f', '-Af', '-c', '100'],
            cwd='/ws', dry_run=True,
        )


SPEC_LINES_WITHOUT_REVIEW = [
    '# A Perforce Change Specification.',
    'Change:\t100',
    '',
    'Client:\tmyclient',
    '',
    'User:\tmyuser',
    '',
    'Status:\tpending',
    '',
    'Description:',
    '\tMy description',
    '',
    'Files:',
    '\t//depot/foo.txt\t# edit',
]

SPEC_LINES_WITH_REVIEW = [
    'Description:',
    '\tMy description',
    '\t#review',
    '',
    'Files:',
]


class TestP4AddReviewKeywordToChangelist(unittest.TestCase):
    @mock.patch('git_p4son.lib.run')
    def test_adds_review_keyword(self, mock_run):
        # First call: p4 change -o (read spec), second: p4 change -i (write spec)
        mock_run.side_effect = [
            make_run_result(stdout=SPEC_LINES_WITHOUT_REVIEW.copy()),
            make_run_result(),
        ]
        add_review_keyword_to_changelist('100', '/ws')
        # Verify p4 change -i was called with updated spec
        call_kwargs = mock_run.call_args_list[1]
        spec_input = call_kwargs.kwargs.get('input')
        self.assertIn('#review', spec_input)

    @mock.patch('git_p4son.lib.run')
    def test_already_has_review_keyword(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=SPEC_LINES_WITH_REVIEW.copy())
        add_review_keyword_to_changelist('100', '/ws')

    @mock.patch('git_p4son.lib.run')
    def test_get_spec_failure(self, mock_run):
        mock_run.side_effect = CommandError('p4 change failed')
        with self.assertRaises(CommandError):
            add_review_keyword_to_changelist('100', '/ws')

    @mock.patch('git_p4son.lib.run')
    def test_dry_run(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=SPEC_LINES_WITHOUT_REVIEW.copy())
        add_review_keyword_to_changelist(
            '100', '/ws', dry_run=True)


if __name__ == '__main__':
    unittest.main()
