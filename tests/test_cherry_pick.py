"""Tests for git_p4son.cherry_pick."""

import contextlib
import io
import os
import unittest

from git_p4son.cherry_pick import cherry_pick_command
from git_p4son.cli import create_parser
from git_p4son.git import SyncState, git_last_synced_changelist, sync_state
from git_p4son.sync import sync_command
from tests.helpers import P4RepoTestCase


class CherryPickTestCase(P4RepoTestCase):
    """main synced to CL 100 (a.txt a1, b.txt b1), with 101 (a2), 102 (b2) and 103 (a3, c.txt) submitted since."""

    def build_workspace(self):
        super().build_workspace()
        self.write_config('[depot]\nroot = "//myclient"\n')
        self.p4.submit(101, {'a.txt': 'a2\n'})
        self.p4.submit(102, {'b.txt': 'b2\n'})
        self.p4.submit(103, {'a.txt': 'a3\n', 'c.txt': 'c1\n'})

    def cherry_pick(self, *argv):
        return self.run_command(cherry_pick_command, ['cherry-pick', *argv])

    def cherry_pick_fails(self, *argv, message):
        return self.command_fails(cherry_pick_command, ['cherry-pick', *argv], message)

    def subjects(self, revision='main'):
        """The subjects after the setup's sync commit, oldest first."""
        return self.git('log', '--reverse', '--format=%s', f'{revision}').splitlines()[1:]

    def files(self):
        return {name: self.read(name) for name in ('a.txt', 'b.txt', 'c.txt')
                if os.path.exists(os.path.join(self.ws, name))}


class TestCherryPick(CherryPickTestCase):
    def test_syncs_only_the_changelist_s_files_and_commits_them(self):
        out = self.cherry_pick('102')
        self.assertEqual(self.files(), {'a.txt': 'a1\n', 'b.txt': 'b2\n'})
        self.assertEqual(self.subjects(), [
                         'git-p4son: p4 sync //myclient/...@=102'])
        self.assertEqual(self.p4.synced(), ['@=102'])
        self.assertIn("# Syncing the files of cherry-pick CL 102\n", out)

    def test_the_last_synced_changelist_stays(self):
        self.cherry_pick('102')
        self.assertEqual(git_last_synced_changelist(self.ws), 100)
        self.assertEqual(sync_state(self.ws), SyncState(100, (102,)))

    def test_several_changelists_get_a_commit_each(self):
        self.cherry_pick('102', '103')
        self.assertEqual(
            self.files(), {'a.txt': 'a3\n', 'b.txt': 'b2\n', 'c.txt': 'c1\n'})
        self.assertEqual(self.subjects(), ['git-p4son: p4 sync //myclient/...@=102',
                                           'git-p4son: p4 sync //myclient/...@=103'])

    def test_a_later_cherry_pick_keeps_the_earlier_ones(self):
        """The catch-up pass syncs to the last synced changelist, then the cherry-picks since it."""
        self.cherry_pick('102')
        self.p4.calls.clear()
        self.cherry_pick('103')
        self.assertEqual(self.p4.synced(), ['@100', '@=102', '@=103'])
        self.assertEqual(
            self.files(), {'a.txt': 'a3\n', 'b.txt': 'b2\n', 'c.txt': 'c1\n'})
        self.assertEqual(sync_state(self.ws), SyncState(100, (102, 103)))

    def test_a_deleted_file_is_deleted(self):
        self.p4.submit(104, {'b.txt': None})
        self.cherry_pick('104')
        self.assertNotIn('b.txt', self.files())
        self.assertNotIn('b.txt', self.git('ls-files').splitlines())

    def test_from_another_branch_it_commits_on_the_sync_branch_and_returns(self):
        self.commit_feature()
        out = self.cherry_pick('102')
        self.assertEqual(self.git('branch', '--show-current'), 'feature')
        self.assertEqual(self.subjects(), [
                         'git-p4son: p4 sync //myclient/...@=102'])
        self.assertIn('main is now at CL 100 with cherry-picks 102. Run "git rebase main" to bring feature up to date',
                      out)

    def test_the_sync_branch_setting_names_the_branch(self):
        self.git('branch', 'trunk')
        self.write_config(
            '[depot]\nroot = "//myclient"\n\n[sync]\nbranch = "trunk"\n')
        self.cherry_pick('102')
        self.assertEqual(self.subjects('trunk'), [
                         'git-p4son: p4 sync //myclient/...@=102'])
        self.assertEqual(self.subjects('main'), [])

    def test_a_changelist_without_files_in_the_workspace_warns(self):
        self.p4.submit(104, {})
        out = self.cherry_pick('104')
        self.assertIn('CL 104 changes no files in the workspace', out)
        self.assertNotIn('Unparsable', out)

    def test_a_changelist_already_at_its_revisions_warns(self):
        self.cherry_pick('102')
        out = self.cherry_pick('102')
        self.assertIn('CL 102 changes no files in the workspace', out)
        self.assertNotIn('Unparsable', out)

    def test_dry_run_changes_nothing(self):
        out = self.cherry_pick('102', '103', '--dry-run')
        self.assertEqual(self.p4.synced(), [])
        self.assertEqual(self.subjects(), [])
        self.assertIn('# Cherry-pick sequence\n[ok] 102 103\n', out)


class TestCherryPickBeforeAnEarlierOne(CherryPickTestCase):
    """Only the last full sync bounds a cherry-pick, so one found to be needed later can still be picked."""

    def test_an_older_changelist_is_picked_after_a_newer_one(self):
        self.cherry_pick('103')
        out = self.cherry_pick('102')
        self.assertEqual(
            self.files(), {'a.txt': 'a3\n', 'b.txt': 'b2\n', 'c.txt': 'c1\n'})
        self.assertEqual(sync_state(self.ws), SyncState(100, (103, 102)))
        self.assertIn('CL 102 is older than the cherry-picked CL 103: a file both changed goes to its revision in '
                      'CL 102', out)

    def test_a_file_both_changed_goes_to_the_older_one_s_revision(self):
        self.cherry_pick('103')
        self.cherry_pick('101')
        self.assertEqual(
            self.files(), {'a.txt': 'a2\n', 'b.txt': 'b1\n', 'c.txt': 'c1\n'})

    def test_the_catch_up_replays_the_cherry_picks_in_their_order(self):
        self.cherry_pick('103')
        self.cherry_pick('101')
        self.p4.calls.clear()
        self.cherry_pick('102')
        self.assertEqual(self.p4.synced()[-3:], ['@=103', '@=101', '@=102'])
        self.assertEqual(
            self.files(), {'a.txt': 'a2\n', 'b.txt': 'b2\n', 'c.txt': 'c1\n'})

    def test_changelists_in_any_order_in_one_run(self):
        out = self.cherry_pick('103', '102')
        self.assertEqual(self.subjects(), ['git-p4son: p4 sync //myclient/...@=103',
                                           'git-p4son: p4 sync //myclient/...@=102'])
        self.assertIn('CL 102 is older than the cherry-picked CL 103', out)


class TestCherryPickRefused(CherryPickTestCase):
    def test_a_changelist_at_or_before_the_last_synced_one(self):
        for changelist in ('100', '99'):
            with self.subTest(changelist=changelist):
                self.cherry_pick_fails(changelist, message=f'Cannot cherry-pick CL {changelist}, at or before the '
                                       'last synced CL 100')

    def test_a_changelist_given_twice(self):
        self.cherry_pick_fails('102', '103', '102',
                               message='CL 102 is given twice')

    def test_a_changelist_that_is_not_submitted(self):
        self.p4.pending['200'] = ['Work in progress']
        self.cherry_pick_fails('200', message='CL 200 is not submitted')

    def test_a_changelist_that_does_not_exist(self):
        self.cherry_pick_fails('300', message='CL 300 does not exist')

    def test_a_branch_without_a_sync(self):
        self.git('commit', '--amend', '-q', '-m', 'Initial')
        self.cherry_pick_fails('102', message='No previous sync found')

    def test_allow_backwards_is_not_an_option(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            create_parser().parse_args(
                ['cherry-pick', '102', '--allow-backwards'])

    def test_nothing_changes_when_refused(self):
        self.cherry_pick_fails('99', message='Cannot cherry-pick')
        self.assertEqual(self.p4.synced(), [])
        self.assertEqual(self.subjects(), [])


class TestSyncAfterCherryPick(CherryPickTestCase):
    def sync(self, *argv):
        return self.run_command(sync_command, ['sync', *argv])

    def test_a_sync_past_the_cherry_pick_keeps_its_files(self):
        self.cherry_pick('102')
        self.sync('103')
        self.assertEqual(
            self.files(), {'a.txt': 'a3\n', 'b.txt': 'b2\n', 'c.txt': 'c1\n'})
        self.assertEqual(git_last_synced_changelist(self.ws), 103)
        self.assertEqual(sync_state(self.ws), SyncState(103))

    def test_a_sync_before_the_cherry_pick_takes_its_files_back(self):
        self.cherry_pick('103')
        out = self.sync('101')
        self.assertEqual(self.files(), {'a.txt': 'a2\n', 'b.txt': 'b1\n'})
        self.assertEqual(git_last_synced_changelist(self.ws), 101)
        self.assertIn(
            'Syncing to CL 101 takes back the cherry-picks of CL 103', out)

    def test_a_sync_to_the_last_synced_changelist_names_the_cherry_picks(self):
        self.cherry_pick('102')
        out = self.sync('100')
        self.assertIn(
            'Already at CL 100 with cherry-picks 102, nothing to sync', out)

    def test_last_synced_resyncs_the_cherry_picks_too(self):
        self.cherry_pick('102')
        self.p4.calls.clear()
        self.sync('last-synced')
        self.assertEqual(self.p4.synced(), ['@100', '@=102'])
        self.assertEqual(self.files(), {'a.txt': 'a1\n', 'b.txt': 'b2\n'})
        self.assertEqual(self.git('status', '--porcelain'), '')


if __name__ == '__main__':
    unittest.main()
