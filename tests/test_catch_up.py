"""Tests for the catch-up commit update inserts after a sync, so the sync's changes get a Swarm patch of their own."""

import os
import stat
import subprocess
import unittest

from git_p4son.common import CommandError
from git_p4son.lib import edit_and_shelve
from git_p4son.new import new_command
from git_p4son.update import update_command
from git_p4son.git import insert_empty_commit
from tests.helpers import captured_output, GitRepoTestCase, P4RepoTestCase, recorded, start_patch

M = ''.join(f'{line}\n' for line in range(1, 9))


class CatchUpTestCase(P4RepoTestCase):
    """CL 102, from branch feature's Edit m top and Add n, shelved as a review if REVIEW; main may then sync CL 103."""

    REVIEW = True

    def build_workspace(self):
        super().build_workspace()
        self.commit_sync(101, {'m.txt': M})
        self.git('switch', '-q', '-c', 'feature')
        self.edit('m.txt', 'top\n' + M.split('\n', 1)[1])
        self.commit('Edit m top')
        self.edit('n.txt', 'n1\n')
        self.commit('Add n')
        review = ['--review'] if self.REVIEW else []
        self.run_command(
            new_command, ['new', 'feat', '-m', 'Feature', '-b', 'main', *review])

    def setUp(self):
        super().setUp()
        start_patch(self, 'git_p4son.per_commit.time.sleep')
        self.p4.calls.clear()

    def sync_main(self, files, rebase=True):
        """Sync CL 103 onto main, then rebase feature onto it unless not rebase, as sync and git rebase would."""
        self.git('switch', '-q', 'main')
        for path in files:
            # Read-only, as synced files are, so the fake p4's noclobber lets the sync replace them.
            if os.path.exists(os.path.join(self.ws, path)):
                os.chmod(os.path.join(self.ws, path), stat.S_IRUSR)
        self.commit_sync(103, files)
        self.git('switch', '-q', 'feature')
        if rebase:
            self.git('rebase', '-q', 'main')

    def add_o(self):
        self.edit('o.txt', 'o1\n')
        self.commit('Add o')

    def update(self, *argv):
        return self.run_command(update_command, ['update', 'feat', *argv])

    def entries(self):
        """The CL's commit list."""
        return [line for line in self.p4.pending['102'] if line[:1].isdigit()]

    def subjects(self):
        return self.git('log', '--reverse', '--format=%s', 'main..HEAD').splitlines()


class TestCatchUp(CatchUpTestCase):
    """main syncs CL 103, changing m.txt's last line, which Edit m top's file holds."""

    def test_per_commit_shelves_the_catch_up_before_the_new_commits(self):
        self.sync_main({'m.txt': M.replace('8\n', 'eight\n')})
        self.add_o()
        self.update('--per-commit', '--shelve')
        self.assertEqual(self.subjects(), [
                         'Edit m top', 'Add n', 'Rebase onto CL 103', 'Add o'])
        self.assertEqual(self.entries(), [
                         '1. Edit m top', '2. Add n', '3. Rebase onto CL 103', '4. Add o'])
        self.assertEqual(len(self.shelves()), 2)
        self.assertEqual(self.read('m.txt'), 'top\n' +
                         M.split('\n', 1)[1].replace('8\n', 'eight\n'))
        self.assertEqual(recorded('102', self.ws).base_commit,
                         self.git('rev-parse', 'main'))

    def test_a_plain_update_shelves_the_catch_up_then_the_tip(self):
        self.sync_main({'m.txt': M.replace('8\n', 'eight\n')})
        self.add_o()
        self.update('--shelve')
        self.assertEqual(self.entries(), [
                         '1. Edit m top', '2. Add n', '3. Rebase onto CL 103', '4. Add o'])
        self.assertEqual(len(self.shelves()), 2)

    def test_a_catch_up_with_nothing_new_is_its_own_shelf(self):
        self.sync_main({'m.txt': M.replace('8\n', 'eight\n')})
        self.update('--shelve')
        self.assertEqual(self.entries(), [
                         '1. Edit m top', '2. Add n', '3. Rebase onto CL 103'])
        self.assertEqual(len(self.shelves()), 1)

    def test_a_sync_that_touches_no_file_of_the_cl_needs_none(self):
        self.sync_main({'other.txt': 'x1\n'})
        self.add_o()
        self.update('--per-commit', '--shelve')
        self.assertEqual(self.entries(), [
                         '1. Edit m top', '2. Add n', '3. Add o'])
        self.assertEqual(recorded('102', self.ws).base_commit,
                         self.git('rev-parse', 'main'))

    def test_an_update_without_shelve_leaves_the_catch_up_for_the_next_shelve(self):
        self.sync_main({'m.txt': M.replace('8\n', 'eight\n')})
        self.add_o()
        self.update()
        self.assertEqual(self.subjects(), ['Edit m top', 'Add n', 'Add o'])
        self.edit('p.txt', 'p1\n')
        self.commit('Add p')
        self.update('--per-commit', '--shelve')
        self.assertEqual(self.entries()[-2:],
                         ['4. Rebase onto CL 103', '5. Add p'])

    def test_a_refused_run_leaves_the_branch_as_it_was(self):
        self.sync_main({'m.txt': M.replace('8\n', 'eight\n')})
        self.add_o()
        self.command_fails(update_command, ['update', 'feat', '--per-commit', '--shelve', '-b', 'main'],
                           message='Leave out -b')
        self.write_file('stray.txt')
        self.command_fails(
            update_command, ['update', 'feat', '--shelve'], message='not clean')
        self.assertEqual(self.subjects(), ['Edit m top', 'Add n', 'Add o'])

    def test_a_branch_built_on_the_recreated_commits_gets_a_warning(self):
        self.sync_main({'m.txt': M.replace('8\n', 'eight\n')})
        self.add_o()
        self.git('branch', 'above', 'HEAD')
        out = self.update('--per-commit', '--shelve')
        self.assertIn(
            'above still builds on the commits before "Rebase onto CL 103"', out)

    def test_a_sync_the_branch_isn_t_rebased_onto_yet_waits_for_the_rebase(self):
        """The shelf only changes once the branch is on the new base, so the catch-up is due then."""
        self.sync_main({'m.txt': M.replace('8\n', 'eight\n')}, rebase=False)
        self.add_o()
        self.update('--shelve')
        self.assertEqual(self.subjects(), ['Edit m top', 'Add n', 'Add o'])
        self.git('rebase', '-q', 'main')
        self.edit('p.txt', 'p1\n')
        self.commit('Add p')
        self.update('--per-commit', '--shelve')
        self.assertEqual(self.entries()[-2:],
                         ['4. Rebase onto CL 103', '5. Add p'])

    def test_a_sync_of_a_file_the_change_renames_is_caught_up(self):
        """Perforce still has b.txt where the change moves it, so the sync's edit of it reaches b2.txt's shelf."""
        self.git('mv', 'b.txt', 'b2.txt')
        self.commit('Move b')
        self.update('--shelve')
        self.sync_main({'b.txt': 'b2\n'})
        self.update('--shelve')
        self.assertEqual(self.entries()[-1], '4. Rebase onto CL 103')

    def test_a_failed_tip_shelve_says_to_run_it_again(self):
        self.sync_main({'m.txt': M.replace('8\n', 'eight\n')})
        self.add_o()
        self.fail_shelve(2)
        self.command_fails(update_command, ['update', 'feat', '--shelve'],
                           message='p4 said no\nAfter fixing the problem, run "git p4son update 102 --shelve" again')

    def fail_shelve(self, failing):
        """Make the edit_and_shelve call numbered failing raise, once."""
        real = edit_and_shelve
        calls = []

        def fail(*args, **kwargs):
            calls.append(args)
            if len(calls) == failing:
                raise CommandError('p4 said no')
            return real(*args, **kwargs)
        start_patch(self, 'git_p4son.update.edit_and_shelve', side_effect=fail)

    def test_a_failed_catch_up_shelve_is_split_off_again_by_the_rerun(self):
        self.sync_main({'m.txt': M.replace('8\n', 'eight\n')})
        self.add_o()
        self.fail_shelve(1)
        self.command_fails(update_command, ['update', 'feat', '--shelve'],
                           message='run "git p4son update 102 --shelve" again')
        self.update('--shelve')
        self.assertEqual(self.subjects(), [
                         'Edit m top', 'Add n', 'Rebase onto CL 103', 'Add o'])
        self.assertEqual(self.entries(), [
                         '1. Edit m top', '2. Add n', '3. Rebase onto CL 103', '4. Add o'])
        self.assertEqual(len(self.shelves()), 2)
        self.assertEqual(recorded('102', self.ws).catch_up, '')

    def test_the_rerun_advice_keeps_the_flags_and_the_message(self):
        self.sync_main({'m.txt': M.replace('8\n', 'eight\n')})
        self.add_o()
        self.fail_shelve(1)
        self.command_fails(update_command, ['update', 'feat', '--shelve', '--no-commit-list', '-m', 'Feature2'],
                           message='run "git p4son update 102 --no-commit-list --shelve --message=Feature2" again')

    def test_a_dry_run_says_what_it_would_insert(self):
        self.sync_main({'m.txt': M.replace('8\n', 'eight\n')})
        out = self.update('--shelve', '--dry-run')
        self.assertIn('Would insert "Rebase onto CL 103" after "Add n"', out)
        self.assertEqual(self.subjects(), ['Edit m top', 'Add n'])


class TestCatchUpNeverShelved(CatchUpTestCase):
    """The CL was created without shelving, so it has no patches to catch up."""

    REVIEW = False

    def build_workspace(self):
        # Defined here, so this class builds and caches a workspace of its own, without the review.
        super().build_workspace()

    def test_the_first_shelve_needs_no_catch_up(self):
        self.assertEqual(recorded('102', self.ws).base_commit, '')
        self.sync_main({'m.txt': M.replace('8\n', 'eight\n')})
        self.update('--shelve')
        self.assertEqual(self.subjects(), ['Edit m top', 'Add n'])
        self.assertEqual(recorded('102', self.ws).base_commit,
                         self.git('rev-parse', 'main'))


class TestInsertEmptyCommit(GitRepoTestCase):
    """git.insert_empty_commit on raw commit objects, which it copies with their parent swapped."""

    def build_workspace(self):
        super().build_workspace()
        self.commit('Base')

    def write_commit(self, headers, message):
        """Write a commit on HEAD with extra raw headers and a message in bytes, and move the branch to it."""
        head = self.git('rev-parse', 'HEAD')
        tree = self.git('rev-parse', 'HEAD^{tree}')
        raw = (f'tree {tree}\nparent {head}\nauthor T <t@x> 1760000000 +0000\ncommitter T <t@x> 1760000000 +0000\n'
               .encode() + headers + b'\n' + message)
        commit = subprocess.run(['git', 'hash-object', '-t', 'commit', '-w', '--stdin'], cwd=self.ws, input=raw,
                                capture_output=True, check=True).stdout.decode().strip()
        self.git('update-ref', 'HEAD', commit)

    def test_a_signature_is_dropped_and_another_encoding_kept(self):
        base = self.git('rev-parse', 'HEAD')
        signature = b'gpgsig -----BEGIN PGP SIGNATURE-----\n abc\n -----END PGP SIGNATURE-----\n'
        self.write_commit(b'encoding ISO-8859-1\n' + signature,
                          'Caf\u00e9\n'.encode('latin-1'))
        with captured_output():
            insert_empty_commit(base, 'Rebase', self.ws)
        raw = subprocess.run(['git', 'cat-file', 'commit', 'HEAD'], cwd=self.ws, capture_output=True,
                             check=True).stdout
        self.assertNotIn(b'gpgsig', raw)
        self.assertIn(b'encoding ISO-8859-1\n', raw)
        self.assertTrue(raw.endswith('Caf\u00e9\n'.encode('latin-1')))
        self.assertEqual(
            self.git('log', '-1', '--format=%s', 'HEAD~1'), 'Rebase')


if __name__ == '__main__':
    unittest.main()
