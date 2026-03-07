"""Tests for git_p4son.config module."""

import os
import tempfile
import unittest

from git_p4son import CONFIG_DIR
from git_p4son.config import config_path, get_depot_root, load_config, save_config


class TestConfigPath(unittest.TestCase):
    def test_returns_expected_path(self):
        result = config_path('/workspace')
        self.assertEqual(result, f'/workspace/{CONFIG_DIR}/config.toml')


class TestLoadConfig(unittest.TestCase):
    def test_missing_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            self.assertEqual(load_config(tmpdir), {})

    def test_reads_toml(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = config_path(tmpdir)
            os.makedirs(os.path.dirname(path))
            with open(path, 'w') as f:
                f.write('[depot]\nroot = "//my-workspace"\n')
            config = load_config(tmpdir)
            self.assertEqual(config, {'depot': {'root': '//my-workspace'}})


class TestSaveConfig(unittest.TestCase):
    def test_creates_file_and_dirs(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {'depot': {'root': '//my-workspace'}}
            save_config(tmpdir, config)
            self.assertTrue(os.path.exists(config_path(tmpdir)))

    def test_round_trip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {'depot': {'root': '//my-workspace/Engine/Source'}}
            save_config(tmpdir, config)
            loaded = load_config(tmpdir)
            self.assertEqual(loaded, config)


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


if __name__ == '__main__':
    unittest.main()
