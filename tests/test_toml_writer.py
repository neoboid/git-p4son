"""Tests for git_p4son.toml_writer module."""

import tomllib
import unittest

from git_p4son.toml_writer import format_toml


class TestFormatToml(unittest.TestCase):
    def test_top_level_scalars(self):
        self.assertEqual(format_toml({'changelist': 123456}),
                         'changelist = 123456\n')

    def test_scalars_come_before_tables(self):
        self.assertEqual(format_toml({'core': {'writable': True}, 'name': 'x'}),
                         'name = "x"\n[core]\nwritable = true\n\n')

    def test_nested_tables_and_quoted_keys(self):
        self.assertEqual(
            format_toml(
                {'hooks': {'extension-associations': {'.ps1': 'pwsh'}}}),
            '[hooks]\n\n[hooks.extension-associations]\n".ps1" = "pwsh"\n\n')

    def test_round_trips_through_tomllib(self):
        data = {'change': 'k"\\x', 'changelist': 1,
                'sync': {'split-users': ['alice', 'bob'], 'flag': False}}
        self.assertEqual(tomllib.loads(format_toml(data)), data)

    def test_unsupported_value_raises(self):
        with self.assertRaises(ValueError):
            format_toml({'when': None})


if __name__ == '__main__':
    unittest.main()
