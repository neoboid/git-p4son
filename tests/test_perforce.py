"""Tests for git_p4son.perforce module."""

import unittest
from unittest import mock

from git_p4son.perforce import (
    P4ClientSpec, P4FileInfo, get_client_spec, is_binary_file_type,
    p4_fstat_file_info, p4_sync_preview,
    parse_ztag_multi_output, parse_ztag_output,
)
from tests.helpers import make_run_result


class TestParseZtagOutput(unittest.TestCase):
    def test_parses_key_value_lines(self):
        lines = [
            '... Client my-ws',
            '... Root /home/user/workspace',
            '... Options noallwrite clobber nocompress',
        ]
        result = parse_ztag_output(lines)
        self.assertEqual(result, {
            'Client': 'my-ws',
            'Root': '/home/user/workspace',
            'Options': 'noallwrite clobber nocompress',
        })

    def test_skips_non_tagged_lines(self):
        lines = [
            '... Client my-ws',
            '',
            'some other line',
            '... Root /ws',
        ]
        result = parse_ztag_output(lines)
        self.assertEqual(result, {'Client': 'my-ws', 'Root': '/ws'})

    def test_empty_value(self):
        lines = ['... Description']
        result = parse_ztag_output(lines)
        self.assertEqual(result, {'Description': ''})

    def test_empty_lines(self):
        self.assertEqual(parse_ztag_output([]), {})


class TestParseZtagMultiOutput(unittest.TestCase):
    def test_single_record(self):
        lines = [
            '... depotFile //depot/foo.txt',
            '... action edit',
            '... change default',
        ]
        result = parse_ztag_multi_output(lines)
        self.assertEqual(result, [
            {'depotFile': '//depot/foo.txt', 'action': 'edit', 'change': 'default'},
        ])

    def test_multiple_records(self):
        lines = [
            '... depotFile //depot/a.txt',
            '... action edit',
            '',
            '... depotFile //depot/b.txt',
            '... action add',
        ]
        result = parse_ztag_multi_output(lines)
        self.assertEqual(result, [
            {'depotFile': '//depot/a.txt', 'action': 'edit'},
            {'depotFile': '//depot/b.txt', 'action': 'add'},
        ])

    def test_empty_lines(self):
        self.assertEqual(parse_ztag_multi_output([]), [])

    def test_no_records(self):
        lines = ['some other output', '']
        self.assertEqual(parse_ztag_multi_output(lines), [])


class TestP4ClientSpec(unittest.TestCase):
    def test_clobber_enabled(self):
        spec = P4ClientSpec(
            name='ws', root='/ws',
            options=['noallwrite', 'clobber', 'nocompress'], stream=None)
        self.assertTrue(spec.clobber)

    def test_clobber_disabled(self):
        spec = P4ClientSpec(
            name='ws', root='/ws',
            options=['noallwrite', 'noclobber', 'nocompress'], stream=None)
        self.assertFalse(spec.clobber)


class TestP4SyncPreview(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run_with_output')
    def test_returns_local_file_paths(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '//depot/foo.txt#1 - added as /ws/foo.txt',
            '//depot/bar.txt#3 - updating /ws/bar.txt',
            '//depot/old.txt#2 - deleted as /ws/old.txt',
        ])
        result = p4_sync_preview(100, '//depot', '/ws')
        self.assertEqual(result, ['/ws/foo.txt', '/ws/bar.txt', '/ws/old.txt'])

    @mock.patch('git_p4son.perforce.run_with_output')
    def test_empty_sync(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '//depot/...@100 - file(s) up-to-date.',
        ])
        result = p4_sync_preview(100, '//depot', '/ws')
        self.assertEqual(result, [])

    @mock.patch('git_p4son.perforce.run_with_output')
    def test_passes_sync_n_flag(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[])
        p4_sync_preview(100, '//depot', '/ws')
        cmd = mock_rwo.call_args[0][0]
        self.assertEqual(cmd, ['p4', 'sync', '-n', '//depot/...@100'])

    @mock.patch('git_p4son.perforce.run_with_output')
    def test_skips_unparsable_lines(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '//depot/foo.txt#1 - added as /ws/foo.txt',
            'some random output',
        ])
        result = p4_sync_preview(100, '//depot', '/ws')
        self.assertEqual(result, ['/ws/foo.txt'])


class TestIsBinaryFileType(unittest.TestCase):
    def test_text_types(self):
        self.assertFalse(is_binary_file_type('text'))
        self.assertFalse(is_binary_file_type('text+x'))
        self.assertFalse(is_binary_file_type('text+kx'))
        self.assertFalse(is_binary_file_type('unicode'))

    def test_binary_types(self):
        self.assertTrue(is_binary_file_type('binary'))
        self.assertTrue(is_binary_file_type('binary+l'))
        self.assertTrue(is_binary_file_type('binary+Swl'))
        self.assertTrue(is_binary_file_type('ubinary'))
        self.assertTrue(is_binary_file_type('ubinary+x'))


class TestP4FstatFileInfo(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_returns_type_and_digest(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... clientFile /ws/foo.txt',
            '... headType text',
            '... digest ABC123',
            '',
            '... clientFile /ws/bar.bin',
            '... headType binary+l',
        ])
        result = p4_fstat_file_info(['/ws/foo.txt', '/ws/bar.bin'], '/ws')
        self.assertEqual(result['/ws/foo.txt'].head_type, 'text')
        self.assertEqual(result['/ws/foo.txt'].digest, 'ABC123')
        self.assertEqual(result['/ws/bar.bin'].head_type, 'binary+l')
        self.assertIsNone(result['/ws/bar.bin'].digest)

    def test_empty_input(self):
        result = p4_fstat_file_info([], '/ws')
        self.assertEqual(result, {})


class TestGetClientSpec(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_valid_workspace(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... Client my-ws',
            '... Update 2026/02/28 09:26:06',
            '... Root /home/user/workspace',
            '... Options noallwrite clobber nocompress',
            '... Stream //projects/main',
        ])
        spec = get_client_spec('/ws')
        self.assertIsNotNone(spec)
        self.assertEqual(spec.name, 'my-ws')
        self.assertEqual(spec.root, '/home/user/workspace')
        self.assertTrue(spec.clobber)
        self.assertEqual(spec.stream, '//projects/main')

    @mock.patch('git_p4son.perforce.run')
    def test_no_stream(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... Client my-ws',
            '... Update 2026/02/28 09:26:06',
            '... Root /ws',
            '... Options noallwrite clobber nocompress',
        ])
        spec = get_client_spec('/ws')
        self.assertIsNone(spec.stream)

    @mock.patch('git_p4son.perforce.run')
    def test_not_in_workspace(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... Client luxon',
            '... Owner andreas',
            '... Root /home/user',
            '... Options noallwrite noclobber nocompress',
        ])
        spec = get_client_spec('/ws')
        self.assertIsNone(spec)
