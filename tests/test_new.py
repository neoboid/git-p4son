"""Tests for git_p4son.new module."""

import unittest
from unittest import mock

from git_p4son.git import LocalChanges
from git_p4son.new import new_command


def _args(**overrides):
    defaults = dict(workspace_dir='/ws', alias=None, force=False,
                    dry_run=True, message='Msg', base_branch='main',
                    no_edit=False, review=False, shelve=False)
    defaults.update(overrides)
    return mock.Mock(**defaults)


class TestNewCommandDryRun(unittest.TestCase):
    """Dry run must walk every optional step without executing anything.

    run() is deliberately left unmocked: all calls that reach it must
    carry dry_run=True, and the placeholder changelist must survive
    command-line rendering (a None changelist used to crash there)."""

    @mock.patch('git_p4son.perforce.get_changelist_for_file',
                return_value=None)
    @mock.patch('git_p4son.lib.get_local_changes')
    @mock.patch('git_p4son.lib.get_enumerated_commit_lines_since',
                return_value=['1. Commit'])
    def test_dry_run_with_edit_review_and_shelve(self, _lines, mock_changes,
                                                 _opened):
        changes = LocalChanges()
        changes.adds = ['new.txt']
        changes.mods = ['mod.txt']
        changes.dels = ['gone.txt']
        mock_changes.return_value = changes

        rc = new_command(_args(review=True, shelve=True))
        self.assertEqual(rc, 0)

    @mock.patch('git_p4son.lib.get_enumerated_commit_lines_since',
                return_value=[])
    def test_dry_run_reports_invalid_alias(self, _lines):
        rc = new_command(_args(alias='bad alias!', no_edit=True))
        self.assertEqual(rc, 1)

    @mock.patch('git_p4son.new.alias_exists', return_value=True)
    @mock.patch('git_p4son.lib.get_enumerated_commit_lines_since',
                return_value=[])
    def test_dry_run_reports_existing_alias(self, _lines, _exists):
        rc = new_command(_args(alias='taken', no_edit=True))
        self.assertEqual(rc, 1)


if __name__ == '__main__':
    unittest.main()
