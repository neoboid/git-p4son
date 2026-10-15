"""Tests for git_p4son.perforce module."""

import sys
import unittest
from unittest import mock

from git_p4son.common import CommandError, RunError
from git_p4son.perforce import (
    ClobberMode,
    extract_description_lines,
    get_changelist_spec,
    get_client_spec,
    get_existing_p4_users,
    get_latest_changelist,
    get_opened_files,
    get_opened_files_in_changelist,
    get_p4_user,
    get_submitted_changes,
    get_writable_files,
    is_always_writable_file_type,
    is_binary_file_type,
    opened_lookup,
    p4_depot_root_exists,
    p4_get_opened_files,
    p4_head_types,
    p4_revert_unchanged,
    p4_shelve_changelist,
    p4_sync,
    p4_sync_preview,
    P4ClientSpec,
    P4SyncAbortError,
    P4SyncClobberError,
    P4SyncOutputProcessor,
    P4SyncPreviewFile,
    parse_p4_sync_line,
    parse_ztag_multi_output,
    parse_ztag_output,
    replace_description_in_spec,
)
from tests.helpers import make_run_result, SAMPLE_SPEC, SAMPLE_SPEC_NO_COMMITS


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

    def test_multiline_value_is_continuation_not_record_boundary(self):
        lines = [
            '... change 12345',
            '... desc Fix the bug',
            'with a second line',
            '',
            '... change 12346',
            '... desc Other',
        ]
        result = parse_ztag_multi_output(lines)
        self.assertEqual(result, [
            {'change': '12345', 'desc': 'Fix the bug\nwith a second line'},
            {'change': '12346', 'desc': 'Other'},
        ])

    def test_empty_lines(self):
        self.assertEqual(parse_ztag_multi_output([]), [])

    def test_no_records(self):
        lines = ['some other output', '']
        self.assertEqual(parse_ztag_multi_output(lines), [])


class TestP4DepotRootExists(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_valid_root(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=['Change 12345 on 2024/01/01'])
        self.assertTrue(p4_depot_root_exists('//my-ws', '/ws'))
        mock_run.assert_called_once_with(
            ['p4', '-L', 'en', 'changes', '-m1', '-s', 'submitted', '//my-ws/...'],
            cwd='/ws')

    @mock.patch('git_p4son.perforce.run', side_effect=CommandError('p4 error'))
    def test_invalid_root(self, mock_run):
        self.assertFalse(p4_depot_root_exists('//bad-root', '/ws'))


class TestP4ClientSpec(unittest.TestCase):
    def test_allwrite_enabled(self):
        spec = P4ClientSpec(
            name='ws', root='/ws',
            options=['allwrite', 'noclobber', 'nocompress'],
            line_end='local')
        self.assertTrue(spec.allwrite)

    def test_allwrite_disabled(self):
        spec = P4ClientSpec(
            name='ws', root='/ws',
            options=['noallwrite', 'noclobber', 'nocompress'],
            line_end='local')
        self.assertFalse(spec.allwrite)

    def test_clobber_mode(self):
        cases = [
            (['noallwrite', 'noclobber'], ClobberMode.NOCLOBBER),
            (['noallwrite', 'clobber'], ClobberMode.CLOBBER),
            (['allwrite', 'noclobber'], ClobberMode.ALLWRITE),
            (['allwrite', 'clobber'], ClobberMode.CLOBBER),
        ]
        for options, expected in cases:
            spec = P4ClientSpec(name='ws', root='/ws',
                                options=options, line_end='local')
            self.assertIs(spec.clobber_mode, expected, options)

    @mock.patch('git_p4son.perforce.sys')
    def test_uses_crlf_local_on_windows(self, mock_sys):
        mock_sys.platform = 'win32'
        spec = P4ClientSpec(
            name='ws', root='/ws', options=[],
            line_end='local')
        self.assertTrue(spec.uses_crlf)

    @mock.patch('git_p4son.perforce.sys')
    def test_uses_crlf_local_on_unix(self, mock_sys):
        mock_sys.platform = 'linux'
        spec = P4ClientSpec(
            name='ws', root='/ws', options=[],
            line_end='local')
        self.assertFalse(spec.uses_crlf)

    def test_uses_crlf_win(self):
        spec = P4ClientSpec(
            name='ws', root='/ws', options=[],
            line_end='win')
        self.assertTrue(spec.uses_crlf)

    def test_uses_crlf_unix(self):
        spec = P4ClientSpec(
            name='ws', root='/ws', options=[],
            line_end='unix')
        self.assertFalse(spec.uses_crlf)


class TestP4SyncPreview(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run_with_stream_output')
    def test_returns_local_file_paths(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '//depot/foo.txt#1 - added as /ws/foo.txt',
            '//depot/bar.txt#3 - updating /ws/bar.txt',
            '//depot/old.txt#2 - deleted as /ws/old.txt',
        ])
        result = p4_sync_preview(100, '//depot', '/ws')
        self.assertEqual(result, [
            P4SyncPreviewFile(mode='add', filepath='/ws/foo.txt'),
            P4SyncPreviewFile(mode='upd', filepath='/ws/bar.txt'),
            P4SyncPreviewFile(mode='del', filepath='/ws/old.txt'),
        ])

    @mock.patch('git_p4son.perforce.run_with_stream_output')
    def test_empty_sync(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '//depot/...@100 - file(s) up-to-date.',
        ])
        result = p4_sync_preview(100, '//depot', '/ws')
        self.assertEqual(result, [])

    @mock.patch('git_p4son.perforce.run_with_stream_output')
    def test_passes_sync_n_flag(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[])
        p4_sync_preview(100, '//depot', '/ws')
        cmd = mock_rwo.call_args[0][0]
        self.assertEqual(
            cmd, ['p4', '-L', 'en', 'sync', '-n', '//depot/...@100'])

    @mock.patch('git_p4son.perforce.run_with_stream_output')
    def test_skips_unparsable_lines(self, mock_rwo):
        mock_rwo.return_value = make_run_result(stdout=[
            '//depot/foo.txt#1 - added as /ws/foo.txt',
            'some random output',
        ])
        result = p4_sync_preview(100, '//depot', '/ws')
        self.assertEqual(result, [
            P4SyncPreviewFile(mode='add', filepath='/ws/foo.txt'),
        ])


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


class TestIsAlwaysWritableFileType(unittest.TestCase):
    def test_without_w_modifier(self):
        self.assertFalse(is_always_writable_file_type('text'))
        self.assertFalse(is_always_writable_file_type('binary'))
        self.assertFalse(is_always_writable_file_type('binary+x'))
        self.assertFalse(is_always_writable_file_type('text+kx'))

    def test_with_w_modifier(self):
        self.assertTrue(is_always_writable_file_type('text+w'))
        self.assertTrue(is_always_writable_file_type('binary+w'))
        self.assertTrue(is_always_writable_file_type('binary+Swl'))
        self.assertTrue(is_always_writable_file_type('binary+xw'))


class TestP4HeadTypes(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_returns_head_type(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... clientFile /ws/foo.txt',
            '... headType text',
            '',
            '... clientFile /ws/bar.bin',
            '... headType binary+l',
        ])
        result = p4_head_types(['/ws/foo.txt', '/ws/bar.bin'], '/ws')
        self.assertEqual(result['/ws/foo.txt'], 'text')
        self.assertEqual(result['/ws/bar.bin'], 'binary+l')

    def test_empty_input(self):
        result = p4_head_types([], '/ws')
        self.assertEqual(result, {})


class TestGetClientSpec(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_valid_workspace(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... Client my-ws',
            '... Update 2026/02/28 09:26:06',
            '... Root /home/user/workspace',
            '... Options noallwrite clobber nocompress',
            '... LineEnd local',
            '... Stream //projects/main',
        ])
        spec = get_client_spec('/ws')
        self.assertIsNotNone(spec)
        self.assertEqual(spec.name, 'my-ws')
        self.assertEqual(spec.root, '/home/user/workspace')
        self.assertIs(spec.clobber_mode, ClobberMode.CLOBBER)
        self.assertEqual(spec.line_end, 'local')

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


class TestGetSubmittedChanges(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_parses_and_sorts_changes(self, mock_run):
        # p4 lists newest first; the sequence builder needs oldest first.
        mock_run.return_value = make_run_result(stdout=[
            '... change 105',
            '... user andreas',
            '... desc Fix the thing',
            '',
            '... change 102',
            '... user colleague',
            '... desc Some other work',
            '',
        ])
        changes = get_submitted_changes('//myclient', 100, 105, '/ws')
        self.assertEqual([(c.change, c.user) for c in changes],
                         [(102, 'colleague'), (105, 'andreas')])
        mock_run.assert_called_once_with(
            ['p4', '-L', 'en', '-ztag', 'changes', '-s', 'submitted',
             '//myclient/...@100,@105'], cwd='/ws')

    @mock.patch('git_p4son.perforce.run')
    def test_no_changes_in_range(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        self.assertEqual(get_submitted_changes('//myclient', 100, 105, '/ws'),
                         [])


class TestGetP4User(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_reads_user_name(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... userName andreas',
            '... clientName my-ws',
        ])
        self.assertEqual(get_p4_user('/ws'), 'andreas')
        mock_run.assert_called_once_with(
            ['p4', '-L', 'en', '-ztag', 'info'], cwd='/ws')

    @mock.patch('git_p4son.perforce.run')
    def test_missing_user_name(self, mock_run):
        mock_run.return_value = make_run_result(stdout=['... clientName x'])
        self.assertIsNone(get_p4_user('/ws'))

    @mock.patch('git_p4son.perforce.run')
    def test_user_unknown_to_the_server(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=['... userName *unknown*'])
        self.assertIsNone(get_p4_user('/ws'))


class TestGetExistingP4Users(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_returns_existing_users_as_spelled_by_the_server(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... User Alice', '... FullName Alice A', '',
            '... User bob', '... FullName Bob B',
        ])
        self.assertEqual(get_existing_p4_users(['alice', 'bob'], '/ws'),
                         ['Alice', 'bob'])
        mock_run.assert_called_once_with(
            ['p4', '-L', 'en', '-ztag', 'users', 'alice', 'bob'], cwd='/ws',
            fail_on_returncode=False)

    @mock.patch('git_p4son.perforce.run')
    def test_unknown_users_are_left_out(self, mock_run):
        mock_run.return_value = make_run_result(
            returncode=1, stdout=['... User bob'],
            stderr=['nobody - no such user(s).'])
        self.assertEqual(get_existing_p4_users(['nobody', 'bob'], '/ws'),
                         ['bob'])

    @mock.patch('git_p4son.perforce.run')
    def test_other_failures_raise(self, mock_run):
        mock_run.return_value = make_run_result(
            returncode=1, stderr=['Perforce client error:',
                                  'Connect to server failed'])
        with self.assertRaises(RunError) as ctx:
            get_existing_p4_users(['bob'], '/ws')
        self.assertIn('Connect to server failed', ctx.exception.stderr)


class TestP4Sync(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run_with_stream_output',
                return_value=mock.Mock(elapsed=None))
    def test_success(self, mock_rwo):
        p4_sync(12345, '//myclient', '/ws')
        self.assertEqual(mock_rwo.call_args.args[0],
                         ['p4', '-L', 'en', 'sync', '//myclient/...@12345'])
        self.assertFalse(mock_rwo.call_args.kwargs['keep_stdout'])

    @mock.patch('git_p4son.perforce.run_with_stream_output')
    def test_clobber_errors_raise_with_the_refused_files(self, mock_rwo):
        mock_rwo.side_effect = RunError(
            'p4 sync failed', returncode=1,
            stderr=["Can't clobber writable file /ws/a.txt",
                    "Can't clobber writable file /ws/b.txt"])
        with self.assertRaises(P4SyncClobberError) as ctx:
            p4_sync(12345, '//myclient', '/ws')
        self.assertEqual(ctx.exception.refused, ['/ws/a.txt', '/ws/b.txt'])
        self.assertEqual(ctx.exception.returncode, 1)

    @mock.patch('git_p4son.perforce.run_with_stream_output')
    def test_other_errors_raise_as_they_are(self, mock_rwo):
        mock_rwo.side_effect = RunError(
            'p4 sync failed', returncode=1, stderr=['some other error'])
        with self.assertRaises(RunError) as ctx:
            p4_sync(12345, '//myclient', '/ws')
        self.assertNotIsInstance(ctx.exception, P4SyncClobberError)


class TestParseP4SyncLine(unittest.TestCase):
    def test_added_file(self):
        mode, filename = parse_p4_sync_line(
            '//depot/foo.txt#1 - added as /ws/foo.txt')
        self.assertEqual(mode, 'add')
        self.assertEqual(filename, '/ws/foo.txt')

    def test_deleted_file(self):
        mode, filename = parse_p4_sync_line(
            '//depot/foo.txt#2 - deleted as /ws/foo.txt')
        self.assertEqual(mode, 'del')
        self.assertEqual(filename, '/ws/foo.txt')

    def test_updated_file(self):
        mode, filename = parse_p4_sync_line(
            '//depot/foo.txt#3 - updating /ws/foo.txt')
        self.assertEqual(mode, 'upd')
        self.assertEqual(filename, '/ws/foo.txt')

    def test_clobber_file(self):
        mode, filename = parse_p4_sync_line(
            "Can't clobber writable file /ws/foo.txt")
        self.assertEqual(mode, 'clb')
        self.assertEqual(filename, '/ws/foo.txt')

    def test_unparsable_line(self):
        mode, filename = parse_p4_sync_line('some random output')
        self.assertIsNone(mode)
        self.assertIsNone(filename)


class TestGetWritableFiles(unittest.TestCase):
    def test_extracts_writable_files(self):
        stderr = [
            "Can't clobber writable file /ws/a.txt",
            "Can't clobber writable file /ws/b.txt",
            "some other error",
        ]
        result = get_writable_files(stderr)
        self.assertEqual(result, ['/ws/a.txt', '/ws/b.txt'])

    def test_empty_stderr(self):
        self.assertEqual(get_writable_files([]), [])


class TestP4GetOpenedFiles(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_clean(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        self.assertEqual(p4_get_opened_files('//depot', '/ws'), [])

    @mock.patch('git_p4son.perforce.run')
    def test_returns_workspace_relative_client_path(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/foo.txt',
            '... path /ws/foo.txt',
            '... action edit',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('foo.txt', 'modify')])

    @mock.patch('git_p4son.perforce.run')
    def test_add(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/new.txt',
            '... path /ws/new.txt',
            '... action add',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('new.txt', 'add')])

    @mock.patch('git_p4son.perforce.run')
    def test_delete(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/old.txt',
            '... path /ws/old.txt',
            '... action delete',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('old.txt', 'delete')])

    @mock.patch('git_p4son.perforce.run')
    def test_move_add(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/new.txt',
            '... path /ws/new.txt',
            '... action move/add',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [('new.txt', 'add')])

    @mock.patch('git_p4son.perforce.run')
    def test_multiple_files(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/a.txt',
            '... path /ws/a.txt',
            '... action edit',
            '',
            '... depotFile //depot/b.txt',
            '... path /ws/b.txt',
            '... action add',
        ])
        result = p4_get_opened_files('//depot', '/ws')
        self.assertEqual(result, [
            ('a.txt', 'modify'),
            ('b.txt', 'add'),
        ])

    @mock.patch('git_p4son.perforce.run')
    def test_normalizes_slashes(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... depotFile //depot/dir/foo.txt',
            r'... path C:\ws\dir\foo.txt',
            '... action add',
        ])
        result = p4_get_opened_files('//depot', r'C:\ws')
        self.assertEqual(result, [('dir/foo.txt', 'add')])

    @mock.patch('git_p4son.perforce.run')
    def test_command_failure(self, mock_run):
        mock_run.side_effect = RunError('p4 opened failed')
        with self.assertRaises(RunError):
            p4_get_opened_files('//depot', '/ws')


class TestGetLatestChangelist(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_success(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... change 54321',
            '... time 1704067200',
            '... user user',
            '... client ws',
        ])
        cl = get_latest_changelist('//myclient', '/ws')
        self.assertEqual(cl, 54321)
        mock_run.assert_called_once_with(
            ['p4', '-L', 'en', '-ztag', 'changes', '-m1', '-s', 'submitted',
             '//myclient/...#head'], cwd='/ws')

    @mock.patch('git_p4son.perforce.run')
    def test_no_changes_found(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[])
        with self.assertRaises(CommandError):
            get_latest_changelist('//myclient', '/ws')


class TestP4SyncOutputProcessor(unittest.TestCase):
    def test_tracks_added_file(self):
        processor = P4SyncOutputProcessor()
        processor('//depot/foo.txt#1 - added as /ws/foo.txt', sys.stdout)
        self.assertEqual(processor.stats['add'], 1)

    def test_tracks_deleted_file(self):
        processor = P4SyncOutputProcessor()
        processor('//depot/foo.txt#2 - deleted as /ws/foo.txt', sys.stdout)
        self.assertEqual(processor.stats['del'], 1)

    def test_up_to_date_message(self):
        processor = P4SyncOutputProcessor()
        processor('//...@12345 - file(s) up-to-date.', sys.stdout)
        self.assertEqual(sum(processor.stats.values()), 0)

    def test_failed_rename_aborts(self):
        processor = P4SyncOutputProcessor()
        with self.assertRaises(P4SyncAbortError):
            processor('rename: failed to rename D:\\ws\\a.uasset after 10 '
                      'attempts: Cannot create a file when that file '
                      'already exists.', sys.stderr)


class TestExtractDescriptionLines(unittest.TestCase):
    def test_extracts_multiline_description(self):
        lines = extract_description_lines(SAMPLE_SPEC)
        self.assertEqual(
            lines, ['Fix the login bug', '1. Add validation', '2. Fix redirect'])

    def test_extracts_simple_description(self):
        lines = extract_description_lines(SAMPLE_SPEC_NO_COMMITS)
        self.assertEqual(lines, ['Just a message'])

    def test_empty_spec(self):
        lines = extract_description_lines('')
        self.assertEqual(lines, [])


class TestReplaceDescriptionInSpec(unittest.TestCase):
    def test_replaces_description(self):
        new_spec = replace_description_in_spec(
            SAMPLE_SPEC, ['New description', 'Line 2'])
        self.assertIn('\tNew description\n', new_spec)
        self.assertIn('\tLine 2\n', new_spec)
        self.assertNotIn('Fix the login bug', new_spec)

    def test_preserves_other_fields(self):
        new_spec = replace_description_in_spec(SAMPLE_SPEC, ['Replaced'])
        self.assertIn('Change:\t12345', new_spec)
        self.assertIn('Files:', new_spec)


class TestGetChangelistSpec(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_success(self, mock_run):
        mock_run.return_value = make_run_result(
            stdout=SAMPLE_SPEC.splitlines())
        spec = get_changelist_spec('12345', '/ws')
        self.assertEqual(spec, SAMPLE_SPEC)

    @mock.patch('git_p4son.perforce.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = RunError('Changelist not found')
        with self.assertRaises(RunError):
            get_changelist_spec('99999', '/ws')


class TestGetOpenedFiles(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_one_query_for_all_files(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... path /ws/a.txt',
            '... action edit',
            '... change 100',
            '',
            '... path /ws/sub/b.txt',
            '... action add',
            '... change default',
        ])
        opened = get_opened_files(['a.txt', 'sub/b.txt', 'c.txt'], '/ws')
        self.assertEqual(opened, {'a.txt': ('100', 'edit'),
                                  'sub/b.txt': ('default', 'add')})
        mock_run.assert_called_once()
        self.assertEqual(mock_run.call_args.args[0],
                         ['p4', '-L', 'en', '-x', '-', '-ztag', 'fstat', '-Ro', '-Op',
                          '-T', 'path,clientFile,action,change'])
        self.assertEqual(mock_run.call_args.kwargs['input'],
                         'a.txt\nsub/b.txt\nc.txt')

    @mock.patch('git_p4son.perforce.run')
    def test_no_files_makes_no_query(self, mock_run):
        self.assertEqual(get_opened_files([], '/ws'), {})
        mock_run.assert_not_called()

    @mock.patch('git_p4son.perforce.log')
    @mock.patch('git_p4son.perforce.run')
    def test_unopened_and_new_files_are_not_errors(self, mock_run, mock_log):
        mock_run.return_value = make_run_result(returncode=1, stderr=[
            'c.txt - file(s) not opened on this client.',
            'new.txt - no such file(s).',
        ])
        self.assertEqual(get_opened_files(['c.txt', 'new.txt'], '/ws'), {})
        mock_log.warning.assert_not_called()

    @mock.patch('git_p4son.perforce.log')
    @mock.patch('git_p4son.perforce.run')
    def test_other_errors_warn(self, mock_run, mock_log):
        mock_run.return_value = make_run_result(
            returncode=1, stderr=['Perforce password (P4PASSWD) invalid.'])
        self.assertEqual(get_opened_files(['a.txt'], '/ws'), {})
        mock_log.warning.assert_called_once()

    @mock.patch('git_p4son.perforce.run')
    def test_windows_paths(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... path D:\\p4\\Games\\Script\\a.as',
            '... action edit',
            '... change 100',
        ])
        opened = get_opened_files(['Script/a.as'], 'D:\\p4\\Games')
        self.assertEqual(opened, {'Script/a.as': ('100', 'edit')})


class TestOpenedLookup(unittest.TestCase):
    def test_exact_match(self):
        lookup = opened_lookup({'a.txt': ('1', 'edit')})
        self.assertEqual(lookup('a.txt'), ('1', 'edit'))

    def test_case_differs(self):
        """p4 may spell the path in the client's case."""
        lookup = opened_lookup({'Source/A.cpp': ('1', 'edit')})
        self.assertEqual(lookup('source/a.cpp'), ('1', 'edit'))

    def test_exact_match_wins_over_case_fold(self):
        lookup = opened_lookup({'a.txt': ('1', 'edit'),
                                'A.txt': ('2', 'add')})
        self.assertEqual(lookup('A.txt'), ('2', 'add'))

    def test_not_opened(self):
        self.assertIsNone(opened_lookup({'a.txt': ('1', 'edit')})('b.txt'))


class TestGetOpenedFilesInChangelist(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_lists_files_opened_in_changelist(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... path /ws/a.txt',
            '... action edit',
            '',
            '... path /ws/sub/b.txt',
            '... action add',
        ])
        files = get_opened_files_in_changelist('100', '/ws')
        self.assertEqual(files, [('a.txt', 'edit'), ('sub/b.txt', 'add')])
        self.assertEqual(
            mock_run.call_args.args[0],
            ['p4', '-L', 'en', '-ztag', 'fstat', '-Ro', '-e', '100', '-Op',
             '-T', 'path,clientFile,action', '...'])

    @mock.patch('git_p4son.perforce.run')
    def test_skips_files_outside_workspace(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... path /elsewhere/a.txt',
            '... action edit',
        ])
        self.assertEqual(get_opened_files_in_changelist('100', '/ws'), [])

    @mock.patch('git_p4son.perforce.run')
    def test_windows_paths(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '... path D:\\p4\\Games\\Script\\a.as',
            '... action edit',
        ])
        files = get_opened_files_in_changelist('100', 'D:\\p4\\Games')
        self.assertEqual(files, [('Script/a.as', 'edit')])

    @mock.patch('git_p4son.perforce.run')
    def test_record_without_path_is_skipped(self, mock_run):
        """A record without a path is skipped instead of crashing."""
        mock_run.return_value = make_run_result(stdout=[
            '... clientFile //client/a.txt',
            '... action edit',
            '',
            '... action edit',
        ])
        self.assertEqual(get_opened_files_in_changelist('100', '/ws'), [])

    @mock.patch('git_p4son.perforce.log')
    @mock.patch('git_p4son.perforce.run')
    def test_nothing_opened(self, mock_run, mock_log):
        mock_run.return_value = make_run_result(
            returncode=1, stderr=['... - file(s) not opened on this client.'])
        self.assertEqual(get_opened_files_in_changelist('100', '/ws'), [])
        mock_log.warning.assert_not_called()

    @mock.patch('git_p4son.perforce.log')
    @mock.patch('git_p4son.perforce.run')
    def test_error_is_warned_about(self, mock_run, mock_log):
        mock_run.return_value = make_run_result(
            returncode=1, stderr=['Perforce client error: connect failed'])
        self.assertEqual(get_opened_files_in_changelist('100', '/ws'), [])
        mock_log.warning.assert_called_once()


class TestP4RevertUnchanged(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_no_files_runs_nothing(self, mock_run):
        self.assertEqual(p4_revert_unchanged(
            [], '100', '/ws', dry_run=False), [])
        mock_run.assert_not_called()

    @mock.patch('git_p4son.perforce.run')
    def test_reverts_via_stdin_file_list(self, mock_run):
        mock_run.return_value = make_run_result(stdout=[
            '//depot/a.txt#3 - was edit, reverted',
        ])
        reverted = p4_revert_unchanged(
            ['a.txt', 'b.txt'], '100', '/ws', dry_run=False)
        self.assertEqual(reverted, ['//depot/a.txt#3 - was edit, reverted'])
        mock_run.assert_called_once_with(
            ['p4', '-L', 'en', '-x', '-', 'revert', '-a', '-c', '100'],
            cwd='/ws', input='a.txt\nb.txt', fail_on_returncode=False)

    @mock.patch('git_p4son.perforce.run')
    def test_dry_run_previews(self, mock_run):
        """-n is a read-only preview, so it really runs on dry run."""
        mock_run.return_value = make_run_result()
        p4_revert_unchanged(['a.txt'], '100', '/ws', dry_run=True)
        mock_run.assert_called_once_with(
            ['p4', '-L', 'en', '-x', '-', 'revert', '-a', '-n', '-c', '100'],
            cwd='/ws', input='a.txt', fail_on_returncode=False)


class TestP4ShelveChangelist(unittest.TestCase):
    @mock.patch('git_p4son.perforce.run')
    def test_replaces_shelf_leaving_unchanged_files_out(self, mock_run):
        """The shelf is replaced to mirror the open files, leaving unchanged files out."""
        mock_run.return_value = make_run_result()
        p4_shelve_changelist('100', '/ws', dry_run=False)
        self.assertEqual(mock_run.call_args_list, [
            mock.call(['p4', '-L', 'en', 'shelve', '-r', '-a', 'leaveunchanged', '-Af',
                       '-c', '100'],
                      cwd='/ws', dry_run=False),
        ])

    @mock.patch('git_p4son.perforce.run')
    def test_failure(self, mock_run):
        mock_run.side_effect = RunError('shelve failed')
        with self.assertRaises(RunError):
            p4_shelve_changelist('100', '/ws', dry_run=False)

    @mock.patch('git_p4son.perforce.run')
    def test_dry_run(self, mock_run):
        mock_run.return_value = make_run_result()
        p4_shelve_changelist('100', '/ws', dry_run=True)
        mock_run.assert_called_once_with(
            ['p4', '-L', 'en', 'shelve', '-r', '-a', 'leaveunchanged', '-Af',
             '-c', '100'],
            cwd='/ws', dry_run=True,
        )
