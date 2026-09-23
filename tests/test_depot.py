"""Tests for git_p4son.depot module."""

import tempfile
import unittest

from git_p4son.config import save_config
from git_p4son.depot import expand_depot_root, get_depot_root


class TestGetDepotRoot(unittest.TestCase):
    def test_returns_root_when_configured(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            save_config(tmpdir, {'depot': {'root': '//ws/path'}})
            self.assertEqual(get_depot_root(tmpdir), '//ws/path')

    def test_returns_none_when_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertIsNone(get_depot_root(tmpdir))

    def test_returns_none_when_no_root_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            save_config(tmpdir, {'other': {'key': 'value'}})
            self.assertIsNone(get_depot_root(tmpdir))


class TestExpandDepotRoot(unittest.TestCase):
    def test_substitutes_placeholder(self):
        self.assertEqual(
            expand_depot_root('//$(workspace)/Engine/Source', 'my-ws'),
            '//my-ws/Engine/Source')

    def test_substitutes_entire_workspace(self):
        self.assertEqual(
            expand_depot_root('//$(workspace)', 'my-ws'), '//my-ws')

    def test_leaves_concrete_root_unchanged(self):
        self.assertEqual(
            expand_depot_root('//my-ws/Engine', 'other-ws'), '//my-ws/Engine')


if __name__ == '__main__':
    unittest.main()
