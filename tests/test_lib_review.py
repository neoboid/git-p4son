"""Tests for review/shelve functions in git_p4son.perforce module."""

import unittest
from unittest import mock

from git_p4son.common import RunError
from git_p4son.perforce import p4_shelve_changelist
from tests.helpers import make_run_result


class TestP4ShelveChangelist(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_replaces_shelf_leaving_unchanged_files_out(self, mock_run):
        """The shelf is replaced to mirror the open files, leaving unchanged files out."""
        mock_run.return_value = make_run_result()
        p4_shelve_changelist('100', '/ws')
        self.assertEqual(mock_run.call_args_list, [
            mock.call(['p4', 'shelve', '-r', '-a', 'leaveunchanged', '-Af',
                       '-c', '100'],
                      cwd='/ws', dry_run=False),
        ])

    @mock.patch('git_p4son.perforce.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = RunError('shelve failed')
        with self.assertRaises(RunError):
            p4_shelve_changelist('100', '/ws')

    @mock.patch('git_p4son.perforce.run')
    def test_dry_run(self, mock_run):
        mock_run.return_value = make_run_result()
        p4_shelve_changelist('100', '/ws', dry_run=True)
        mock_run.assert_called_once_with(
            ['p4', 'shelve', '-r', '-a', 'leaveunchanged', '-Af',
             '-c', '100'],
            cwd='/ws', dry_run=True,
        )


if __name__ == '__main__':
    unittest.main()
