"""Tests for git_p4son.perforce module."""

import unittest
from unittest import mock

from git_p4son.perforce import P4ClientSpec, get_client_spec, parse_ztag_output
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
