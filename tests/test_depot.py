"""Tests for git_p4son.depot module."""

import tempfile
import unittest
from unittest import mock

from git_p4son.config import save_config
from git_p4son.depot import (
    check_depot_root,
    expand_depot_root,
    get_depot_root,
    subdirectory_depot_root,
)


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


class TestSubdirectoryDepotRoot(unittest.TestCase):
    def test_subdirectory(self):
        result = subdirectory_depot_root('/ws/Engine/Source', '/ws')
        self.assertEqual(result, '//$(workspace)/Engine/Source')

    def test_at_workspace_root(self):
        result = subdirectory_depot_root('/ws', '/ws')
        self.assertIsNone(result)

    def test_nested_subdirectory(self):
        result = subdirectory_depot_root('/ws/a/b/c', '/ws')
        self.assertEqual(result, '//$(workspace)/a/b/c')

    def test_outside_workspace(self):
        result = subdirectory_depot_root('/elsewhere', '/ws')
        self.assertIsNone(result)


class TestCheckDepotRoot(unittest.TestCase):
    @mock.patch('git_p4son.depot.p4_depot_root_exists', return_value=True)
    def test_checks_the_expanded_root(self, mock_exists):
        self.assertTrue(check_depot_root(
            '//$(workspace)/Engine', 'my-ws', '/ws'))
        mock_exists.assert_called_once_with('//my-ws/Engine', '/ws')

    @mock.patch('git_p4son.depot.log')
    @mock.patch('git_p4son.depot.p4_depot_root_exists', return_value=False)
    def test_logs_an_invalid_root(self, _exists, mock_log):
        self.assertFalse(check_depot_root('//$(workspace)', 'my-ws', '/ws'))
        mock_log.error.assert_called_once_with(
            'Depot root //my-ws/... is not valid')
