"""Tests for git_p4son.changelist_store module."""

import unittest

from git_p4son.changelist_store import validate_alias_name


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
