"""Tests for review/shelve functions in git_p4son.lib module."""

import unittest
from unittest import mock

from git_p4son.lib import (
    p4_shelve_changelist,
    add_review_keyword_to_changelist,
)
from tests.helpers import make_run_result


class TestP4ShelveChangelist(unittest.TestCase):
    @mock.patch('git_p4son.lib.run')
    def test_success(self, mock_run):
        mock_run.return_value = make_run_result()
        rc = p4_shelve_changelist('100', '/ws')
        self.assertEqual(rc, 0)
        mock_run.assert_called_once_with(
            ['p4', 'shelve', '-f', '-Af', '-c', '100'],
            cwd='/ws', dry_run=False,
        )

    @mock.patch('git_p4son.lib.run')
    def test_failure(self, mock_run):
        mock_run.return_value = make_run_result(returncode=1)
        rc = p4_shelve_changelist('100', '/ws')
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.lib.run')
    def test_dry_run(self, mock_run):
        mock_run.return_value = make_run_result()
        rc = p4_shelve_changelist('100', '/ws', dry_run=True)
        self.assertEqual(rc, 0)
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
    @mock.patch('git_p4son.lib.subprocess.run')
    @mock.patch('git_p4son.lib.run')
    def test_adds_review_keyword(self, mock_run, mock_subprocess):
        mock_run.return_value = make_run_result(
            stdout=SPEC_LINES_WITHOUT_REVIEW.copy())
        mock_subprocess.return_value = mock.Mock(
            returncode=0, stdout='', stderr='')
        rc = add_review_keyword_to_changelist('100', '/ws')
        self.assertEqual(rc, 0)
        # Verify p4 change -i was called with updated spec
        call_kwargs = mock_subprocess.call_args
        spec_input = call_kwargs.kwargs.get(
            'input') or call_kwargs[1].get('input')
        self.assertIn('#review', spec_input)

    @mock.patch('git_p4son.lib.run')
    def test_already_has_review_keyword(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=SPEC_LINES_WITH_REVIEW.copy())
        rc = add_review_keyword_to_changelist('100', '/ws')
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.lib.run')
    def test_get_spec_failure(self, mock_run):
        mock_run.return_value = make_run_result(returncode=1)
        rc = add_review_keyword_to_changelist('100', '/ws')
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.lib.run')
    def test_dry_run(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=SPEC_LINES_WITHOUT_REVIEW.copy())
        rc = add_review_keyword_to_changelist(
            '100', '/ws', dry_run=True)
        self.assertEqual(rc, 0)


if __name__ == '__main__':
    unittest.main()
