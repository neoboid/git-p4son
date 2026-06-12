"""Tests for git_p4son.changelist_store module."""

import os
import tempfile
import unittest

from git_p4son.changelist_store import (
    delete_changelist_alias,
    load_changelist_alias,
    save_changelist_alias,
    validate_alias_name,
)


class TestAliasStoreValidatesNames(unittest.TestCase):
    """load and delete must validate names like save does - a raw name
    such as ../../somefile must never escape the store directory."""

    def setUp(self):
        self._tempdir = tempfile.TemporaryDirectory()
        self.ws = self._tempdir.name

    def tearDown(self):
        self._tempdir.cleanup()

    def test_save_load_delete_roundtrip(self):
        self.assertTrue(save_changelist_alias('feature', '123', self.ws))
        self.assertEqual(load_changelist_alias('feature', self.ws), '123')
        self.assertTrue(delete_changelist_alias('feature', self.ws))
        self.assertIsNone(load_changelist_alias('feature', self.ws))

    def test_load_rejects_path_traversal(self):
        # The store directory must exist for the relative path to resolve.
        save_changelist_alias('some-alias', '1', self.ws)
        outside = os.path.join(self.ws, 'secret.txt')
        with open(outside, 'w') as f:
            f.write('55555\n')

        # Relative to the store dir this resolves to the file above.
        self.assertIsNone(
            load_changelist_alias('../../secret.txt', self.ws))

    def test_delete_rejects_path_traversal(self):
        save_changelist_alias('some-alias', '1', self.ws)
        outside = os.path.join(self.ws, 'precious.txt')
        with open(outside, 'w') as f:
            f.write('do not delete\n')

        self.assertFalse(
            delete_changelist_alias('../../precious.txt', self.ws))
        self.assertTrue(os.path.exists(outside))


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
        error = validate_alias_name('')
        self.assertIsNotNone(error)
        self.assertIn('empty', error)

    def test_reserved_branch_rejected(self):
        error = validate_alias_name('branch')
        self.assertIsNotNone(error)
        self.assertIn('reserved', error)

    def test_reserved_last_synced_rejected(self):
        error = validate_alias_name('last-synced')
        self.assertIsNotNone(error)
        self.assertIn('reserved', error)

    def test_spaces_rejected(self):
        error = validate_alias_name('Fix a couple of small bugs')
        self.assertIsNotNone(error)
        self.assertIn('Invalid alias name', error)

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

    def test_special_chars_rejected(self):
        for ch in ['*', '?', ':', '"', '|', '<', '>', '\'', '(', ')', '!']:
            with self.subTest(ch=ch):
                self.assertIsNotNone(validate_alias_name(f'foo{ch}bar'))


if __name__ == '__main__':
    unittest.main()
