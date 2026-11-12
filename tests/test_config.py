"""Tests for git_p4son.config module."""

import os
import subprocess
import tempfile
import unittest

from git_p4son import CONFIG_DIR
from git_p4son.config import (
    config_path,
    ConfigError,
    ensure_config_dir,
    get_setting,
    load_config,
    save_config,
    set_setting_in_place,
)
from tests.helpers import init_git_repo, WorkspaceTestCase


def _read_config_gitignore(workspace_dir):
    with open(os.path.join(workspace_dir, CONFIG_DIR, '.gitignore'),
              encoding='utf-8') as f:
        return f.read()


class TestEnsureConfigDir(WorkspaceTestCase):
    def test_creates_dir_ignoring_everything(self):
        ensure_config_dir(self.ws)
        self.assertEqual(_read_config_gitignore(self.ws), '*\n')

    def test_replaces_state_only_gitignore(self):
        """Earlier versions ignored only state.toml."""
        os.makedirs(os.path.join(self.ws, CONFIG_DIR))
        with open(os.path.join(self.ws, CONFIG_DIR, '.gitignore'), 'w',
                  encoding='utf-8') as f:
            f.write('state.toml\n')
        ensure_config_dir(self.ws)
        self.assertEqual(_read_config_gitignore(self.ws), '*\n')

    def test_save_config_ensures_gitignore(self):
        save_config(self.ws, {'depot': {'root': '//ws/...'}})
        self.assertEqual(_read_config_gitignore(self.ws), '*\n')

    def test_git_ignores_config_dir(self):
        init_git_repo(self.ws)
        save_config(self.ws, {'depot': {'root': '//ws/...'}})
        status = subprocess.run(
            ['git', 'status', '--porcelain', '--untracked-files=all'],
            cwd=self.ws, check=True, capture_output=True, text=True)
        self.assertEqual(status.stdout, '')


class TestConfigPath(unittest.TestCase):
    def test_returns_expected_path(self):
        result = config_path('/workspace')
        self.assertEqual(result, os.path.join(
            '/workspace', CONFIG_DIR, 'config.toml'))


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

    def test_preserves_other_sections(self):
        """Saving one section keeps the other sections, like [hooks]."""
        with tempfile.TemporaryDirectory() as tmpdir:
            path = config_path(tmpdir)
            os.makedirs(os.path.dirname(path))
            with open(path, 'w') as f:
                f.write('[depot]\n'
                        'root = "//old-root"\n'
                        '\n'
                        '[hooks.extension-associations]\n'
                        '".py" = ["python", "-u"]\n'
                        '".sh" = "bash"\n')

            save_config(tmpdir, {'depot': {'root': '//new-root'}})

            loaded = load_config(tmpdir)
            self.assertEqual(loaded, {
                'depot': {'root': '//new-root'},
                'hooks': {'extension-associations': {
                    '.py': ['python', '-u'],
                    '.sh': 'bash',
                }},
            })

    def test_round_trip_nested_tables_and_lists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {'hooks': {'extension-associations': {
                '.py': ['python', '-u'],
                '.sh': 'bash',
            }}}
            save_config(tmpdir, config)
            self.assertEqual(load_config(tmpdir), config)

    def test_escapes_quotes_and_backslashes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            config = {'depot': {'root': 'D:\\p4\\"games"'}}
            save_config(tmpdir, config)
            self.assertEqual(load_config(tmpdir), config)

    def test_updates_existing_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            save_config(tmpdir, {'depot': {'root': '//first'}})
            save_config(tmpdir, {'depot': {'root': '//second'}})
            self.assertEqual(
                load_config(tmpdir), {'depot': {'root': '//second'}})


class TestSetSettingInPlace(WorkspaceTestCase):
    """set_setting_in_place edits one line of config.toml and leaves the rest as written."""

    HAND_WRITTEN = (
        '# Workspace settings, edited by hand.\n'
        '[review-tool]\n'
        '\n'
        '# Our reviewer.\n'
        '[review-tool.reviewer]\n'
        'command = [\n'
        '    "C:/tools/reviewer.exe",   # pinned build\n'
        ']\n')

    def text(self):
        with open(config_path(self.ws), encoding='utf-8') as f:
            return f.read()

    def test_inserts_the_key_after_the_header_keeping_the_rest(self):
        self.write_config(self.HAND_WRITTEN)
        set_setting_in_place(self.ws, 'review-tool', 'default', 'reviewer')
        self.assertEqual(self.text(), self.HAND_WRITTEN.replace(
            '[review-tool]\n', '[review-tool]\ndefault = "reviewer"\n', 1))

    def test_replaces_the_line_in_its_own_table_only(self):
        self.write_config(
            '[core]\ndefault = 1\n\n[review-tool]  # tools\n  default = "a"  # old\nx = 2\n')
        set_setting_in_place(self.ws, 'review-tool', 'default', 'b')
        self.assertEqual(self.text(
        ), '[core]\ndefault = 1\n\n[review-tool]  # tools\n  default = "b"\nx = 2\n')

    def test_a_crlf_file_stays_crlf(self):
        ensure_config_dir(self.ws)
        with open(config_path(self.ws), 'w', encoding='utf-8', newline='') as f:
            f.write('[review-tool]\r\n# c\r\n')
        set_setting_in_place(self.ws, 'review-tool', 'default', 'a')
        with open(config_path(self.ws), encoding='utf-8', newline='') as f:
            self.assertEqual(
                f.read(), '[review-tool]\r\ndefault = "a"\r\n# c\r\n')

    def test_a_table_appended_to_a_crlf_file_is_crlf(self):
        ensure_config_dir(self.ws)
        with open(config_path(self.ws), 'w', encoding='utf-8', newline='') as f:
            f.write('[core]\r\nwritable = true\r\n')
        set_setting_in_place(self.ws, 'sync', 'split-users', ['a'])
        with open(config_path(self.ws), encoding='utf-8', newline='') as f:
            self.assertEqual(
                f.read(), '[core]\r\nwritable = true\r\n\r\n[sync]\r\nsplit-users = ["a"]\r\n')

    def test_appends_the_table_when_there_is_none(self):
        self.write_config('[core]\nwritable = true')
        set_setting_in_place(self.ws, 'review-tool', 'default', 'a')
        self.assertEqual(
            self.text(), '[core]\nwritable = true\n\n[review-tool]\ndefault = "a"\n')

    def test_creates_the_file(self):
        set_setting_in_place(self.ws, 'review-tool', 'default', 'a')
        self.assertEqual(self.text(), '[review-tool]\ndefault = "a"\n')
        self.assertEqual(_read_config_gitignore(self.ws), '*\n')

    def test_replaces_a_value_written_over_several_lines(self):
        self.write_config('[sync]\n'
                          '# Who gets a commit of their own.\n'
                          'split-users = [\n'
                          '    "alice",  # lead\n'
                          '    "bob",\n'
                          ']\n'
                          'branch = "main"\n')
        set_setting_in_place(
            self.ws, 'sync', 'split-users', ['carol', '$(user)'])
        self.assertEqual(self.text(), '[sync]\n# Who gets a commit of their own.\nsplit-users = ["carol", "$(user)"]\n'
                         'branch = "main"\n')

    def test_a_bool(self):
        self.write_config('# Mine.\n[core]\nwritable = false\n')
        set_setting_in_place(self.ws, 'core', 'writable', True)
        self.assertEqual(self.text(), '# Mine.\n[core]\nwritable = true\n')

    def test_an_empty_list(self):
        set_setting_in_place(self.ws, 'sync', 'split-users', [])
        self.assertEqual(self.text(), '[sync]\nsplit-users = []\n')

    def test_refuses_a_file_that_cannot_hold_the_setting_leaving_it_alone(self):
        self.write_config('review-tool = "a"\n')
        with self.assertRaisesRegex(ConfigError, 'Set default in \\[review-tool\\] by hand'):
            set_setting_in_place(self.ws, 'review-tool', 'default', 'b')
        self.assertEqual(self.text(), 'review-tool = "a"\n')

    def test_keeps_a_date_in_an_inline_table(self):
        self.write_config('sync = { branch = "main", when = 2026-01-01 }\n')
        set_setting_in_place(self.ws, 'sync', 'split-users', ['a'])
        self.assertEqual(self.text(
        ), 'sync = { branch = "main", when = 2026-01-01, split-users = ["a"] }\n')

    def test_a_quoted_header_and_key(self):
        self.write_config('["sync"]\n"split-users" = ["a"]\n')
        set_setting_in_place(self.ws, 'sync', 'split-users', ['b'])
        self.assertEqual(self.text(), '["sync"]\n"split-users" = ["b"]\n')

    def test_a_dotted_key_at_the_top(self):
        self.write_config(
            '# Mine.\nreview-tool.default = "a"  # old\n\n[core]\nwritable = true\n')
        set_setting_in_place(self.ws, 'review-tool', 'default', 'b')
        self.assertEqual(
            self.text(), '# Mine.\nreview-tool.default = "b"\n\n[core]\nwritable = true\n')

    def test_a_new_key_beside_dotted_keys_is_dotted(self):
        self.write_config(
            'sync.branch = [\n  "x",\n]\n\n[core]\nwritable = true\n')
        set_setting_in_place(self.ws, 'sync', 'split-users', ['a'])
        self.assertEqual(self.text(), 'sync.branch = [\n  "x",\n]\nsync.split-users = ["a"]\n\n'
                         '[core]\nwritable = true\n')

    def test_an_inline_table(self):
        self.write_config(
            'depot = { root = "//a", other = 1 }  # depot\n[core]\nwritable = true\n')
        set_setting_in_place(self.ws, 'depot', 'root', '//b')
        self.assertEqual(
            self.text(), 'depot = { root = "//b", other = 1 }\n[core]\nwritable = true\n')

    def test_a_new_key_in_an_inline_table(self):
        self.write_config('sync = { branch = "main" }\n')
        set_setting_in_place(self.ws, 'sync', 'split-users', ['a'])
        self.assertEqual(
            self.text(), 'sync = { branch = "main", split-users = ["a"] }\n')

    def test_a_table_after_its_sub_tables(self):
        self.write_config('[review-tool.t]\ncommand = ["t"]\n')
        set_setting_in_place(self.ws, 'review-tool', 'default', 't')
        self.assertEqual(
            self.text(), '[review-tool.t]\ncommand = ["t"]\n\n[review-tool]\ndefault = "t"\n')

    def test_a_nested_array_does_not_end_the_table(self):
        self.write_config(
            '[sync]\nother = [\n  ["a"]\n]\nsplit-users = ["x"]\n')
        set_setting_in_place(self.ws, 'sync', 'split-users', ['y'])
        self.assertEqual(
            self.text(), '[sync]\nother = [\n  ["a"]\n]\nsplit-users = ["y"]\n')


class TestLoadConfigInvalid(unittest.TestCase):
    def test_invalid_toml_raises_config_error(self):
        with tempfile.TemporaryDirectory() as ws:
            os.makedirs(os.path.join(ws, CONFIG_DIR))
            with open(config_path(ws), 'w', encoding='utf-8') as f:
                f.write('[sync\n')
            with self.assertRaisesRegex(ConfigError, 'is not valid TOML'):
                load_config(ws)


class TestGetSetting(WorkspaceTestCase):

    def test_default_when_unset(self):
        self.assertEqual(get_setting(self.ws, 'sync', 'x', str, 'd'), 'd')
        self.write_config('[sync]\n')
        self.assertEqual(get_setting(self.ws, 'sync', 'x', str, 'd'), 'd')

    def test_returns_a_value_of_the_expected_type(self):
        self.write_config(
            '[core]\nwritable = true\n[sync]\nusers = ["a", "b"]\n')
        self.assertIs(get_setting(self.ws, 'core',
                      'writable', bool, False), True)
        self.assertEqual(get_setting(self.ws, 'sync', 'users', list[str], []),
                         ['a', 'b'])

    def test_wrong_type_raises(self):
        self.write_config(
            '[core]\nwritable = "true"\n[sync]\nusers = ["a", 1]\n')
        with self.assertRaisesRegex(ConfigError, 'core.writable .* true or false'):
            get_setting(self.ws, 'core', 'writable', bool, False)
        with self.assertRaisesRegex(ConfigError, 'sync.users .* a list of strings'):
            get_setting(self.ws, 'sync', 'users', list[str], [])

    def test_section_not_a_table_raises(self):
        self.write_config('sync = "x"\n')
        with self.assertRaisesRegex(ConfigError, 'sync in config.toml is not a table'):
            get_setting(self.ws, 'sync', 'users', list[str], [])


if __name__ == '__main__':
    unittest.main()
