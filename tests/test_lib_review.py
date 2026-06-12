"""Tests for review/shelve functions in git_p4son.perforce module."""

import unittest
from unittest import mock

from git_p4son.common import CommandError, RunError
from git_p4son.perforce import (
    p4_shelve_changelist,
    add_review_keyword_to_changelist,
)
from tests.helpers import make_run_result


class TestP4ShelveChangelist(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_deletes_stale_shelf_then_shelves(self, mock_run):
        """The shelf is cleared first so it mirrors the open files;
        shelve -f alone leaves stale entries for files no longer open."""
        mock_run.return_value = make_run_result()
        p4_shelve_changelist('100', '/ws')
        self.assertEqual(mock_run.call_args_list, [
            mock.call(['p4', 'shelve', '-d', '-c', '100'],
                      cwd='/ws', dry_run=False, fail_on_returncode=False),
            mock.call(['p4', 'shelve', '-f', '-Af', '-c', '100'],
                      cwd='/ws', dry_run=False),
        ])

    @mock.patch('git_p4son.perforce.run')
    def test_empty_shelf_delete_failure_is_tolerated(self, mock_run):
        """shelve -d exits non-zero when nothing is shelved yet."""
        mock_run.side_effect = [
            make_run_result(returncode=1),
            make_run_result(),
        ]
        p4_shelve_changelist('100', '/ws')
        self.assertEqual(mock_run.call_count, 2)

    @mock.patch('git_p4son.perforce.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = [make_run_result(),
                                RunError('shelve failed')]
        with self.assertRaises(RunError):
            p4_shelve_changelist('100', '/ws')

    @mock.patch('git_p4son.perforce.run')
    def test_dry_run(self, mock_run):
        mock_run.return_value = make_run_result()
        p4_shelve_changelist('100', '/ws', dry_run=True)
        mock_run.assert_called_with(
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
    @mock.patch('git_p4son.perforce.run')
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

    @mock.patch('git_p4son.perforce.run')
    def test_already_has_review_keyword(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=SPEC_LINES_WITH_REVIEW.copy())
        add_review_keyword_to_changelist('100', '/ws')

    @mock.patch('git_p4son.perforce.run')
    def test_get_spec_failure(self, mock_run):
        mock_run.side_effect = RunError('p4 change failed')
        with self.assertRaises(RunError):
            add_review_keyword_to_changelist('100', '/ws')

    @mock.patch('git_p4son.perforce.run')
    def test_dry_run_does_not_touch_the_server(self, mock_run):
        """Dry run must not even fetch the spec - the changelist may be a
        placeholder from a dry-run create."""
        add_review_keyword_to_changelist(
            '<changelist>', '/ws', dry_run=True)
        mock_run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
