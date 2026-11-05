"""Tests for opening git renames as p4 moves, through new, update and review."""

import os

from git_p4son.new import new_command
from git_p4son.update import update_command
from tests.helpers import on_jj, P4RepoTestCase, start_patch


M = ''.join(f'line {number}\n' for number in range(1, 21))
M_EDITED = M.replace('line 7\n', 'line seven\n')


class RenameTestCase(P4RepoTestCase):
    """A depot with m.txt, a file long enough for git to see a rename with an edit as one, synced at CL 101."""

    def build_workspace(self):
        super().build_workspace()
        self.commit_sync(101, {'m.txt': M})
        self.git('switch', '-q', '-c', 'feature')

    def setUp(self):
        super().setUp()
        start_patch(self, 'git_p4son.per_commit.time.sleep')

    def new(self, *argv):
        return self.run_command(new_command, ['new', 'feat', '-m', 'Feature', '-b', 'main', *argv])

    def update(self, *argv):
        return self.run_command(update_command, ['update', 'feat', *argv])

    def review(self):
        return self.run_command(new_command, ['review', 'feat', '-m', 'Feature', '-b', 'main'])

    def assert_moved(self, old, new):
        """Assert old is moved to new in CL 102, and nothing else is opened."""
        self.assertEqual(self.p4.opened, {old: (
            '102', 'move/delete'), new: ('102', 'move/add')})

    def moves(self):
        """The renames p4 moved, as (old, new)."""
        return self.p4.moves_made


class TestRenames(RenameTestCase):
    def test_new_opens_a_rename_as_a_move_with_its_edit(self):
        self.move('m.txt', 'src/m.txt', M_EDITED)
        self.commit('Move m')
        self.new('--shelve')
        self.assert_moved('m.txt', 'src/m.txt')
        self.assertEqual(self.p4.shelved['102'], {
                         'm.txt': None, 'src/m.txt': M_EDITED})
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_renames_are_found_whatever_diff_renames_says(self):
        self.git('config', 'diff.renames', 'false')
        self.move('m.txt', 'src/m.txt')
        self.commit('Move m')
        self.new()
        self.assert_moved('m.txt', 'src/m.txt')

    def test_update_keeps_the_move_open(self):
        self.move('m.txt', 'src/m.txt')
        self.commit('Move m')
        self.new()
        self.p4.calls.clear()
        self.p4.moves_made.clear()
        self.update('-b', 'main')
        self.assert_moved('m.txt', 'src/m.txt')
        self.assertEqual(self.moves(), [])
        self.assertFalse(
            [call for call in self.p4.calls if 'revert' in call and '-a' not in call])

    def test_a_dry_run_lists_the_moves(self):
        self.move('m.txt', 'src/m.txt')
        self.commit('Move m')
        out = self.new('--dry-run')
        self.assertIn('Opening for move:\n  m.txt -> src/m.txt', out)
        self.assertEqual(self.p4.opened, {})

    def test_a_rename_undone_reverts_the_move(self):
        self.move('m.txt', 'src/m.txt')
        self.commit('Move m')
        self.new()
        self.move('src/m.txt', 'm.txt')
        self.commit('Move m back')
        self.update()
        self.assertEqual(self.p4.opened, {})
        self.assertEqual(self.read('m.txt'), M)
        self.assertFalse(os.path.exists(os.path.join(self.ws, 'src', 'm.txt')))
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_a_rename_undone_over_the_whole_range_reverts_the_move(self):
        self.move('m.txt', 'src/m.txt')
        self.commit('Move m')
        self.new()
        self.move('src/m.txt', 'm.txt')
        self.commit('Move m back')
        self.update('-b', 'main')
        self.assertEqual(self.p4.opened, {})
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_a_rename_undone_keeps_its_edit(self):
        self.move('m.txt', 'src/m.txt', M_EDITED)
        self.commit('Move m')
        self.new('--shelve')
        self.move('src/m.txt', 'm.txt')
        self.commit('Move m back')
        self.update('--shelve')
        self.assertEqual(self.p4.opened, {'m.txt': ('102', 'edit')})
        self.assertEqual(self.p4.shelved['102'], {'m.txt': M_EDITED})

    def test_review_moves_then_keeps_the_move_through_a_later_edit(self):
        self.move('m.txt', 'src/m.txt')
        self.commit('Move m')
        self.edit('src/m.txt', M_EDITED)
        self.commit('Edit m')
        self.run_command(
            new_command, ['review', 'feat', '-m', 'Feature', '-b', 'main'])
        self.assert_moved('m.txt', 'src/m.txt')
        self.assertEqual(self.p4.shelved['102']['src/m.txt'], M_EDITED)
        self.assertEqual(self.moves(), [('m.txt', 'src/m.txt')])

    def test_review_moves_a_moved_file_again_from_where_it_started(self):
        self.move('m.txt', 'src/m.txt')
        self.commit('Move m')
        self.move('src/m.txt', 'lib/m.txt')
        self.commit('Move m again')
        self.run_command(
            new_command, ['review', 'feat', '-m', 'Feature', '-b', 'main'])
        self.assert_moved('m.txt', 'lib/m.txt')

    def test_a_file_added_earlier_in_the_change_is_renamed_as_an_add(self):
        self.write_file('n.txt', M)
        self.commit('Add n')
        self.move('n.txt', 'src/n.txt')
        self.commit('Move n')
        self.run_command(
            new_command, ['review', 'feat', '-m', 'Feature', '-b', 'main'])
        self.assertEqual(self.p4.opened, {'src/n.txt': ('102', 'add')})
        self.assertEqual(self.moves(), [])


TestRenamesOnJj = on_jj(TestRenames)
