"""Tests for pergit.review module."""

import unittest
from unittest import mock

from pergit.review import (
    p4_shelve_changelist,
    open_changes_for_edit,
    add_review_keyword_to_changelist,
    review_new_command,
    review_update_command,
    review_command,
)
from tests.helpers import make_run_result


class TestP4ShelveChangelist(unittest.TestCase):
    @mock.patch('pergit.review.run')
    def test_success(self, mock_run):
        mock_run.return_value = make_run_result()
        rc = p4_shelve_changelist('100', '/ws')
        self.assertEqual(rc, 0)
        mock_run.assert_called_once_with(
            ['p4', 'shelve', '-f', '-Af', '-c', '100'],
            cwd='/ws', dry_run=False,
        )

    @mock.patch('pergit.review.run')
    def test_failure(self, mock_run):
        mock_run.return_value = make_run_result(returncode=1)
        rc = p4_shelve_changelist('100', '/ws')
        self.assertEqual(rc, 1)

    @mock.patch('pergit.review.run')
    def test_dry_run(self, mock_run):
        mock_run.return_value = make_run_result()
        rc = p4_shelve_changelist('100', '/ws', dry_run=True)
        self.assertEqual(rc, 0)
        mock_run.assert_called_once_with(
            ['p4', 'shelve', '-f', '-Af', '-c', '100'],
            cwd='/ws', dry_run=True,
        )


class TestOpenChangesForEdit(unittest.TestCase):
    @mock.patch('pergit.review.include_changes_in_changelist', return_value=0)
    @mock.patch('pergit.review.get_local_git_changes')
    def test_success(self, mock_get_changes, mock_include):
        mock_changes = mock.Mock()
        mock_get_changes.return_value = (0, mock_changes)
        rc = open_changes_for_edit('HEAD~1', '100', '/ws')
        self.assertEqual(rc, 0)
        mock_include.assert_called_once_with(mock_changes, '100', '/ws', False)

    @mock.patch('pergit.review.get_local_git_changes')
    def test_get_changes_failure(self, mock_get_changes):
        mock_get_changes.return_value = (1, None)
        rc = open_changes_for_edit('HEAD~1', '100', '/ws')
        self.assertEqual(rc, 1)


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
    @mock.patch('pergit.review.subprocess.run')
    @mock.patch('pergit.review.run')
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

    @mock.patch('pergit.review.run')
    def test_already_has_review_keyword(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=SPEC_LINES_WITH_REVIEW.copy())
        rc = add_review_keyword_to_changelist('100', '/ws')
        self.assertEqual(rc, 0)

    @mock.patch('pergit.review.run')
    def test_get_spec_failure(self, mock_run):
        mock_run.return_value = make_run_result(returncode=1)
        rc = add_review_keyword_to_changelist('100', '/ws')
        self.assertEqual(rc, 1)

    @mock.patch('pergit.review.run')
    def test_dry_run(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=SPEC_LINES_WITHOUT_REVIEW.copy())
        rc = add_review_keyword_to_changelist(
            '100', '/ws', dry_run=True)
        self.assertEqual(rc, 0)


class TestReviewNewCommand(unittest.TestCase):
    @mock.patch('pergit.review.p4_shelve_changelist', return_value=0)
    @mock.patch('pergit.review.add_review_keyword_to_changelist', return_value=0)
    @mock.patch('pergit.review.open_changes_for_edit', return_value=0)
    @mock.patch('pergit.review.create_changelist', return_value=(0, '500'))
    @mock.patch('pergit.review.ensure_workspace', return_value='/ws')
    def test_full_flow(self, _ws, mock_create, mock_open, mock_review, mock_shelve):
        args = mock.Mock(message='New review',
                         base_branch='HEAD~1', dry_run=False,
                         alias=None, force=False)
        rc = review_new_command(args)
        self.assertEqual(rc, 0)
        mock_create.assert_called_once_with(
            'New review', 'HEAD~1', '/ws', dry_run=False)
        mock_open.assert_called_once_with('HEAD~1', '500', '/ws', False)
        mock_review.assert_called_once_with('500', '/ws', dry_run=False)
        mock_shelve.assert_called_once_with('500', '/ws', dry_run=False)

    @mock.patch('pergit.review.create_changelist', return_value=(1, None))
    @mock.patch('pergit.review.ensure_workspace', return_value='/ws')
    def test_create_failure(self, _ws, _create):
        args = mock.Mock(message='msg', base_branch='HEAD~1', dry_run=False,
                         alias=None, force=False)
        rc = review_new_command(args)
        self.assertEqual(rc, 1)


class TestReviewUpdateCommand(unittest.TestCase):
    @mock.patch('pergit.review.p4_shelve_changelist', return_value=0)
    @mock.patch('pergit.review.open_changes_for_edit', return_value=0)
    @mock.patch('pergit.review.resolve_changelist', return_value='500')
    @mock.patch('pergit.review.ensure_workspace', return_value='/ws')
    def test_update_without_description(self, _ws, _resolve, mock_open, mock_shelve):
        args = mock.Mock(changelist='500', base_branch='HEAD~1',
                         dry_run=False, description=False)
        rc = review_update_command(args)
        self.assertEqual(rc, 0)
        mock_open.assert_called_once()
        mock_shelve.assert_called_once()

    @mock.patch('pergit.review.p4_shelve_changelist', return_value=0)
    @mock.patch('pergit.review.open_changes_for_edit', return_value=0)
    @mock.patch('pergit.review.update_changelist', return_value=0)
    @mock.patch('pergit.review.resolve_changelist', return_value='500')
    @mock.patch('pergit.review.ensure_workspace', return_value='/ws')
    def test_update_with_description(self, _ws, _resolve, mock_update, mock_open, mock_shelve):
        args = mock.Mock(changelist='500', base_branch='HEAD~1',
                         dry_run=False, description=True)
        rc = review_update_command(args)
        self.assertEqual(rc, 0)
        mock_update.assert_called_once_with(
            '500', 'HEAD~1', '/ws', dry_run=False)

    @mock.patch('pergit.review.resolve_changelist', return_value=None)
    @mock.patch('pergit.review.ensure_workspace', return_value='/ws')
    def test_invalid_changelist(self, _ws, _resolve):
        args = mock.Mock(changelist='abc', base_branch='HEAD~1',
                         dry_run=False, description=False)
        rc = review_update_command(args)
        self.assertEqual(rc, 1)


class TestReviewCommand(unittest.TestCase):
    @mock.patch('pergit.review.review_new_command', return_value=0)
    def test_dispatches_new(self, mock_new):
        args = mock.Mock(review_action='new')
        rc = review_command(args)
        self.assertEqual(rc, 0)
        mock_new.assert_called_once()

    @mock.patch('pergit.review.review_update_command', return_value=0)
    def test_dispatches_update(self, mock_update):
        args = mock.Mock(review_action='update')
        rc = review_command(args)
        self.assertEqual(rc, 0)
        mock_update.assert_called_once()

    def test_no_action(self):
        args = mock.Mock(review_action=None)
        rc = review_command(args)
        self.assertEqual(rc, 1)


if __name__ == '__main__':
    unittest.main()
