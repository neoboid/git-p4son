"""Tests for git_p4son.changelist_store module."""

import os
import tempfile
import unittest

from git_p4son import CONFIG_DIR
from git_p4son.changelist_store import (
    alias_changes,
    alias_for_change,
    find_record,
    Record,
    branch_alias,
    branch_to_alias,
    check_alias_usable,
    delete_changelist_alias,
    list_changelist_aliases,
    load_changelist_alias,
    migrate_changelist_aliases,
    save_changelist_alias,
    update_record,
    validate_alias_name,
)
from git_p4son.common import CommandError
from tests.helpers import WorkspaceTestCase


class TestAliasStoreValidatesNames(WorkspaceTestCase):
    """load and delete validate names like save, so ../../somefile cannot escape the store."""

    def test_save_load_delete_roundtrip(self):
        save_changelist_alias('feature', '123', self.ws, force=False)
        self.assertEqual(load_changelist_alias('feature', self.ws), '123')
        delete_changelist_alias('feature', self.ws)
        with self.assertRaisesRegex(CommandError, 'No changelist alias found'):
            load_changelist_alias('feature', self.ws)

    def test_save_ensures_config_gitignore(self):
        save_changelist_alias('feature', '123', self.ws, force=False)
        gitignore = os.path.join(self.ws, CONFIG_DIR, '.gitignore')
        with open(gitignore, encoding='utf-8') as f:
            self.assertEqual(f.read(), '*\n')

    def test_load_rejects_path_traversal(self):
        # The store directory must exist for the relative path to resolve.
        save_changelist_alias('some-alias', '1', self.ws, force=False)
        outside = os.path.join(self.ws, 'secret.txt')
        with open(outside, 'w') as f:
            f.write('55555\n')

        # Relative to the store dir this resolves to the file above.
        with self.assertRaisesRegex(CommandError, 'Invalid alias name'):
            load_changelist_alias('../../secret.txt', self.ws)

    def test_delete_rejects_path_traversal(self):
        save_changelist_alias('some-alias', '1', self.ws, force=False)
        outside = os.path.join(self.ws, 'precious.txt')
        with open(outside, 'w') as f:
            f.write('do not delete\n')

        with self.assertRaisesRegex(CommandError, 'Invalid alias name'):
            delete_changelist_alias('../../precious.txt', self.ws)
        self.assertTrue(os.path.exists(outside))


class TestAliasStoreLocations(WorkspaceTestCase):
    """The store in .git-p4son/aliases/, with the old .git-p4son/changelists/ still read."""

    def path(self, *parts):
        return os.path.join(self.ws, CONFIG_DIR, *parts)

    def write_old(self, name, content):
        os.makedirs(self.path('changelists'), exist_ok=True)
        with open(self.path('changelists', name), 'w') as f:
            f.write(content)

    def write_new(self, name, content):
        os.makedirs(self.path('aliases'), exist_ok=True)
        with open(self.path('aliases', f'{name}.toml'), 'w') as f:
            f.write(content)

    def test_save_writes_a_toml_file(self):
        save_changelist_alias('feature', '123456', self.ws, force=False)
        with open(self.path('aliases', 'feature.toml'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'changelist = 123456\n')

    def test_save_refuses_a_non_numeric_changelist(self):
        with self.assertRaisesRegex(CommandError, 'Invalid changelist number'):
            save_changelist_alias('feature', 'abc', self.ws, force=False)

    def test_reads_the_old_store(self):
        self.write_old('feature', '100\n')
        self.assertEqual(load_changelist_alias('feature', self.ws), '100')
        self.assertEqual(list_changelist_aliases(
            self.ws), [('feature', '100')])

    def test_reading_leaves_the_old_store_alone(self):
        self.write_old('feature', '100\n')
        load_changelist_alias('feature', self.ws)
        list_changelist_aliases(self.ws)
        self.assertTrue(os.path.exists(self.path('changelists', 'feature')))
        self.assertFalse(os.path.exists(self.path('aliases')))

    def test_the_new_store_wins_over_the_old(self):
        self.write_old('feature', '100\n')
        self.write_old('other', '300\n')
        self.write_new('feature', 'changelist = 200\n')
        self.assertEqual(load_changelist_alias('feature', self.ws), '200')
        self.assertEqual(list_changelist_aliases(self.ws),
                         [('feature', '200'), ('other', '300')])

    def test_an_old_alias_is_not_free(self):
        self.write_old('feature', '100\n')
        with self.assertRaisesRegex(CommandError, 'already exists'):
            check_alias_usable('feature', self.ws, force=False)

    def test_save_removes_the_old_alias_and_the_empty_old_store(self):
        self.write_old('feature', '100\n')
        save_changelist_alias('feature', '200', self.ws, force=True)
        self.assertFalse(os.path.exists(self.path('changelists')))
        self.assertEqual(load_changelist_alias('feature', self.ws), '200')

    def test_save_keeps_the_old_store_while_it_holds_other_aliases(self):
        self.write_old('feature', '100\n')
        self.write_old('other', '300\n')
        save_changelist_alias('feature', '200', self.ws, force=True)
        self.assertEqual(os.listdir(self.path('changelists')), ['other'])

    def test_delete_removes_the_alias_from_both_stores(self):
        self.write_old('feature', '100\n')
        self.write_new('feature', 'changelist = 200\n')
        delete_changelist_alias('feature', self.ws)
        self.assertFalse(os.path.exists(self.path('changelists')))
        self.assertEqual(os.listdir(self.path('aliases')), [])

    def test_delete_an_alias_only_in_the_old_store(self):
        self.write_old('feature', '100\n')
        delete_changelist_alias('feature', self.ws)
        with self.assertRaisesRegex(CommandError, 'No changelist alias found'):
            load_changelist_alias('feature', self.ws)

    def test_migrate_moves_the_old_aliases(self):
        self.write_old('feature', '100\n')
        self.write_old('other', '300\n')
        self.write_new('other', 'changelist = 400\n')
        self.assertEqual(migrate_changelist_aliases(self.ws), 1)
        self.assertFalse(os.path.exists(self.path('changelists')))
        self.assertEqual(list_changelist_aliases(self.ws),
                         [('feature', '100'), ('other', '400')])

    def test_alias_name_with_dots(self):
        save_changelist_alias('feat.v1.0', '123', self.ws, force=False)
        self.assertTrue(os.path.exists(self.path('aliases', 'feat.v1.0.toml')))
        self.assertEqual(load_changelist_alias('feat.v1.0', self.ws), '123')
        self.assertEqual(list_changelist_aliases(
            self.ws), [('feat.v1.0', '123')])

    def test_invalid_toml_fails_to_load_and_is_not_listed(self):
        self.write_new('feature', 'changelist = \n')
        with self.assertRaisesRegex(CommandError, 'is not valid TOML'):
            load_changelist_alias('feature', self.ws)
        self.assertEqual(list_changelist_aliases(self.ws), [])

    def test_a_file_without_a_changelist_number_fails_to_load(self):
        self.write_new('feature', 'changelist = "123"\n')
        with self.assertRaisesRegex(CommandError, 'has no changelist number'):
            load_changelist_alias('feature', self.ws)

    def test_files_that_are_not_aliases_are_not_listed(self):
        self.write_new('feature', 'changelist = 1\n')
        self.write_old('.DS_Store', 'junk')
        with open(self.path('aliases', 'notes.txt'), 'w') as f:
            f.write('changelist = 2\n')
        self.assertEqual(list_changelist_aliases(self.ws), [('feature', '1')])


class TestAliasChanges(WorkspaceTestCase):
    """An alias may record the jj change it was made for, as change = "<full change ID>"."""

    def test_save_records_the_change(self):
        save_changelist_alias('feat', '101', self.ws,
                              force=False, change='kxyz')
        with open(os.path.join(self.ws, CONFIG_DIR, 'aliases', 'feat.toml'), encoding='utf-8') as f:
            self.assertEqual(f.read(), 'changelist = 101\nchange = "kxyz"\n')
        self.assertEqual(load_changelist_alias('feat', self.ws), '101')

    def test_the_alias_for_a_change_is_found(self):
        save_changelist_alias('feat', '101', self.ws,
                              force=False, change='kxyz')
        save_changelist_alias('other', '102', self.ws, force=False)
        self.assertEqual(alias_for_change('kxyz', self.ws), 'feat')
        self.assertIsNone(alias_for_change('lmno', self.ws))
        self.assertEqual(alias_changes(self.ws), {'feat': 'kxyz'})

    def test_saving_a_change_moves_its_record_and_keeps_the_changelist(self):
        save_changelist_alias('feat', '101', self.ws,
                              force=False, change='kxyz')
        save_changelist_alias('next', '102', self.ws,
                              force=False, change='kxyz')
        self.assertEqual(alias_changes(self.ws), {'next': 'kxyz'})
        self.assertEqual(load_changelist_alias('feat', self.ws), '101')


class TestRecord(WorkspaceTestCase):
    """An alias made by new records its CL's base and the identities of the commits since it the CL includes."""

    RECORD = Record(base='main', base_commit='a' * 40,
                    included=['1760781234 +0200', '1760781299 +0200'])

    def read(self, name):
        with open(os.path.join(self.ws, CONFIG_DIR, 'aliases', f'{name}.toml'), encoding='utf-8') as f:
            return f.read()

    def test_save_writes_the_record_beside_the_changelist(self):
        save_changelist_alias('feat', '101', self.ws,
                              force=False, change='kxyz', record=self.RECORD)
        self.assertEqual(self.read('feat'), 'changelist = 101\nchange = "kxyz"\nbase = "main"\n'
                         f'base_commit = "{"a" * 40}"\nincluded = ["1760781234 +0200", "1760781299 +0200"]\n')
        self.assertEqual(find_record('101', self.ws), ('feat', self.RECORD))

    def test_an_alias_without_one_records_none(self):
        save_changelist_alias('feat', '101', self.ws, force=False)
        self.assertIsNone(find_record('101', self.ws))
        self.assertIsNone(find_record('102', self.ws))

    def test_update_keeps_the_changelist_and_the_change(self):
        save_changelist_alias('feat', '101', self.ws,
                              force=False, change='kxyz', record=self.RECORD)
        moved = Record(base='main', base_commit='b' *
                       40, included=['1760781234 +0200'])
        update_record('feat', moved, self.ws)
        self.assertEqual(find_record('101', self.ws), ('feat', moved))
        self.assertEqual(load_changelist_alias('feat', self.ws), '101')
        self.assertEqual(alias_changes(self.ws), {'feat': 'kxyz'})

    def test_moving_the_change_keeps_the_other_alias_s_record(self):
        save_changelist_alias('feat', '101', self.ws,
                              force=False, change='kxyz', record=self.RECORD)
        save_changelist_alias('next', '102', self.ws,
                              force=False, change='kxyz')
        self.assertEqual(find_record('101', self.ws), ('feat', self.RECORD))

    def test_a_record_with_a_missing_or_wrong_key_is_none(self):
        save_changelist_alias('feat', '101', self.ws, force=False)
        # The second is slice 1's count, which was never released.
        for extra in ('base = "main"\nincluded = ["x"]\n',
                      'base = "main"\nbase_commit = "x"\nincluded = 2\nlast = "y"\n',
                      'base = "main"\nbase_commit = "x"\nincluded = [2]\n'):
            with self.subTest(extra=extra):
                self.write_file(os.path.join(
                    CONFIG_DIR, 'aliases', 'feat.toml'), 'changelist = 101\n' + extra)
                self.assertIsNone(find_record('101', self.ws))


class TestBranchToAlias(unittest.TestCase):
    def test_replaces_slashes(self):
        self.assertEqual(branch_to_alias('feat/my-feature'), 'feat-my-feature')

    def test_no_slash_passthrough(self):
        self.assertEqual(branch_to_alias('my-branch'), 'my-branch')

    def test_multiple_slashes(self):
        self.assertEqual(branch_to_alias('a/b/c'), 'a-b-c')


class TestBranchAlias(unittest.TestCase):
    def test_maps_the_branch(self):
        self.assertEqual(branch_alias('feat/x'), 'feat-x')

    def test_reserved_keyword_is_refused(self):
        self.assertIsNone(branch_alias('head'))


class TestSaveChangelistAlias(unittest.TestCase):
    def test_save_refuses_an_existing_alias_unless_forced(self):
        with tempfile.TemporaryDirectory() as ws:
            save_changelist_alias('feature', '123', ws, force=False)
            with self.assertRaisesRegex(CommandError, 'already exists'):
                save_changelist_alias('feature', '456', ws, force=False)
            save_changelist_alias('feature', '456', ws, force=True)
            self.assertEqual(load_changelist_alias('feature', ws), '456')


class TestCheckAliasUsable(unittest.TestCase):
    def test_invalid_name_fails_even_with_force(self):
        with tempfile.TemporaryDirectory() as ws:
            with self.assertRaisesRegex(CommandError, 'Invalid alias name'):
                check_alias_usable('bad name!', ws, force=True)

    def test_existing_alias_needs_force(self):
        with tempfile.TemporaryDirectory() as ws:
            save_changelist_alias('feature', '123', ws, force=False)
            with self.assertRaisesRegex(CommandError, 'already exists'):
                check_alias_usable('feature', ws, force=False)
            check_alias_usable('feature', ws, force=True)

    def test_new_alias_is_usable(self):
        with tempfile.TemporaryDirectory() as ws:
            check_alias_usable('feature', ws, force=False)


class TestValidateAliasName(unittest.TestCase):
    def test_simple_name(self):
        self.assertIsNone(validate_alias_name('my-feature'))

    def test_alphanumeric(self):
        self.assertIsNone(validate_alias_name('feat123'))

    def test_underscores(self):
        self.assertIsNone(validate_alias_name('my_feature'))

    def test_dots_in_middle(self):
        self.assertIsNone(validate_alias_name('feat.v1.0'))

    def test_single_char(self):
        self.assertIsNone(validate_alias_name('a'))

    def test_leading_hyphen_allowed(self):
        self.assertIsNone(validate_alias_name('-foo'))

    def test_empty_rejected(self):
        self.assertIn('empty', validate_alias_name('') or '')

    def test_reserved_branch_rejected(self):
        self.assertIn('reserved', validate_alias_name('branch') or '')

    def test_reserved_last_synced_rejected(self):
        self.assertIn('reserved', validate_alias_name('last-synced') or '')

    def test_reserved_head_rejected(self):
        self.assertIn('reserved', validate_alias_name('head') or '')

    def test_spaces_rejected(self):
        self.assertIn('Invalid alias name', validate_alias_name(
            'Fix a couple of small bugs') or '')

    def test_slash_rejected(self):
        error = validate_alias_name('feat/foo')
        self.assertIsNotNone(error)

    def test_backslash_rejected(self):
        error = validate_alias_name('feat\\foo')
        self.assertIsNotNone(error)

    def test_leading_dot_rejected(self):
        error = validate_alias_name('.hidden')
        self.assertIsNotNone(error)

    def test_trailing_dot_rejected(self):
        error = validate_alias_name('foo.')
        self.assertIsNotNone(error)

    def test_dot_alone_rejected(self):
        self.assertIsNotNone(validate_alias_name('.'))

    def test_double_dot_rejected(self):
        self.assertIsNotNone(validate_alias_name('..'))

    def test_all_digit_rejected(self):
        """An all-digit alias would parse as a changelist number, so it is rejected."""
        self.assertIsNotNone(validate_alias_name('12345'))

    def test_digits_with_letters_allowed(self):
        self.assertIsNone(validate_alias_name('feature123'))

    def test_windows_reserved_names_rejected(self):
        for name in ('CON', 'aux', 'NUL', 'com1', 'LPT9', 'con.txt'):
            self.assertIsNotNone(validate_alias_name(name), name)

    def test_special_chars_rejected(self):
        for ch in ['*', '?', ':', '"', '|', '<', '>', '\'', '(', ')', '!']:
            with self.subTest(ch=ch):
                self.assertIsNotNone(validate_alias_name(f'foo{ch}bar'))


if __name__ == '__main__':
    unittest.main()
