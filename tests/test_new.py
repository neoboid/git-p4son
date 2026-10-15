"""Tests for git_p4son.new module."""

import unittest

from git_p4son.changelist_store import (
    list_changelist_aliases,
    load_changelist_alias,
    save_changelist_alias,
)
from git_p4son.new import new_command
from tests.helpers import P4RepoTestCase


FEATURE_DESCRIPTION = ['Feature', '', 'Changes included:',
                       '1. Change a', '2. Replace b with c']
FEATURE_OPENED = {'a.txt': ('101', 'edit'), 'b.txt': ('101', 'delete'),
                  'c.txt': ('101', 'add')}


class NewTestCase(P4RepoTestCase):
    """new on branch feature, two commits on from main."""

    def setUp(self):
        super().setUp()
        self.commit_feature()

    def new(self, *argv, **overrides):
        """Run new for alias feat with argv; returns everything it printed."""
        return self.run_command(
            new_command, ['new', 'feat', '-m', 'Feature', '-b', 'main', *argv],
            **overrides)

    def new_fails(self, *argv, message):
        self.command_fails(
            new_command, ['new', 'feat', '-m', 'Feature', '-b', 'main', *argv],
            message)


class TestNew(NewTestCase):
    def test_creates_a_changelist_with_the_git_changes(self):
        out = self.new()
        self.assertEqual(self.p4.pending, {'101': FEATURE_DESCRIPTION})
        self.assertEqual(self.p4.opened, FEATURE_OPENED)
        self.assertEqual(self.p4.shelved, {})
        self.assertEqual(load_changelist_alias('feat', self.ws), '101')
        self.assertIn('Created CL 101 (alias=feat)', out)
        self.assertIn('Opened 3 files', out)

    def test_review_adds_the_keyword_and_shelves(self):
        self.new('--review')
        self.assertEqual(self.p4.pending['101'],
                         FEATURE_DESCRIPTION + ['', '#review'])
        self.assertEqual(self.p4.shelved,
                         {'101': {'a.txt': 'a2\n', 'b.txt': None,
                                  'c.txt': 'c1\n'}})

    def test_shelve(self):
        self.new('--shelve')
        self.assertEqual(self.p4.pending['101'], FEATURE_DESCRIPTION)
        self.assertEqual(set(self.p4.shelved['101']),
                         {'a.txt', 'b.txt', 'c.txt'})

    def test_no_commit_list(self):
        self.new('--no-commit-list')
        self.assertEqual(self.p4.pending['101'], ['Feature'])

    def test_moves_files_opened_in_another_changelist(self):
        self.p4.pending['50'] = ['Other']
        self.p4.opened['a.txt'] = ('50', 'edit')
        self.new()
        self.assertEqual(self.p4.opened, FEATURE_OPENED)

    def test_dry_run_changes_nothing(self):
        out = self.new('--dry-run', '--review')
        self.assertEqual(self.p4.pending, {})
        self.assertEqual(self.p4.opened, {})
        self.assertEqual(self.p4.shelved, {})
        self.assertIn('Would create a changelist with this description '
                      '(alias=feat)', out)
        self.assertIn('2. Replace b with c', out)
        self.assertIn('Would shelve the new changelist', out)
        self.assertEqual(list_changelist_aliases(self.ws), [])


class TestNewCleanWorkspace(NewTestCase):
    def test_refuses_a_dirty_workspace_before_creating(self):
        self.edit('a.txt', 'dirty\n')
        for argv in ([], ['--dry-run']):
            with self.subTest(argv=argv):
                self.new_fails(*argv, message='not clean')
        self.assertEqual(self.p4.pending, {})

    def test_untracked_file_counts_as_dirty(self):
        self.write_file('stray.txt', 'stray\n')
        self.new_fails(message='not clean')
        self.assertEqual(self.p4.pending, {})

    def test_no_edit_skips_the_check_and_opens_nothing(self):
        self.edit('a.txt', 'dirty\n')
        self.new('--no-edit')
        self.assertEqual(self.p4.pending, {'101': FEATURE_DESCRIPTION})
        self.assertEqual(self.p4.opened, {})


class TestNewAlias(NewTestCase):
    def test_existing_alias_stops_before_creating(self):
        save_changelist_alias('feat', '1', self.ws, force=False)
        for argv in ([], ['--dry-run']):
            with self.subTest(argv=argv):
                self.new_fails(*argv, message='already exists')
        self.assertEqual(self.p4.pending, {})
        self.assertEqual(load_changelist_alias('feat', self.ws), '1')

    def test_force_replaces_an_existing_alias(self):
        save_changelist_alias('feat', '1', self.ws, force=False)
        self.new('-f')
        self.assertEqual(load_changelist_alias('feat', self.ws), '101')

    def test_invalid_alias_stops_before_creating(self):
        self.command_fails(new_command, ['new', 'bad alias!', '-m', 'Feature'],
                           message='Invalid alias name')
        self.assertEqual(self.p4.pending, {})

    def test_no_alias(self):
        out = self.new('--no-alias', alias=None)
        self.assertIn('Created CL 101\n', out)
        self.assertEqual(self.p4.pending, {'101': FEATURE_DESCRIPTION})


if __name__ == '__main__':
    unittest.main()
