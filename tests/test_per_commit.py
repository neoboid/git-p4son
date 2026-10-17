"""Tests for git_p4son.per_commit module."""

import unittest

from git_p4son.git_backend import GitBackend
from git_p4son.common import CommandError, RunError
from git_p4son.per_commit import run_per_commit
from tests.helpers import captured_output, GitRepoTestCase, start_patch


class TestRunPerCommit(GitRepoTestCase):
    """run_per_commit of update first, then update rest, over the two commits on branch feature."""

    def build_workspace(self):
        super().build_workspace()
        self.commit('Base')
        self.git('switch', '-q', '-c', 'feature')
        self.commit('First commit')
        self.commit('Second commit')
        self.feature_head = self.git('rev-parse', 'HEAD')

    def setUp(self):
        super().setUp()
        self.ran = []
        self.errors = {}
        self.sleep = start_patch(self, 'git_p4son.per_commit.time.sleep')
        start_patch(self, 'git_p4son.cli.update_command',
                    side_effect=self.update)

    def update(self, args):
        """Record (changelist, commit subject, base, message), then raise the error set for the changelist."""
        self.ran.append((args.changelist, self.git(
            'log', '-1', '--format=%s'), args.base_branch, args.message))
        if args.changelist in self.errors:
            raise self.errors[args.changelist]

    def run_steps(self, base_branch='main', **kwargs):
        """Run update first, then update rest, with kwargs; returns everything it printed."""
        kwargs.setdefault('sleep', None)
        kwargs.setdefault('dry_run', False)
        with captured_output() as out:
            run_per_commit(['update', 'first'], ['update', 'rest'], base_branch, self.ws, GitBackend(self.ws),
                           **kwargs)
        return out.getvalue()

    def step_fails(self, **kwargs):
        """Run the steps, expecting a CommandError; returns it."""
        with self.assertRaises(CommandError) as raised:
            self.run_steps(**kwargs)
        return raised.exception

    def test_runs_first_then_rest_with_each_commit_checked_out(self):
        self.run_steps()
        main = self.git('rev-parse', 'main')
        self.assertEqual(self.ran, [('first', 'First commit', main, None),
                                    ('rest', 'Second commit', main, None)])
        self.assert_at('feature', self.feature_head)

    def test_rest_defaults_to_first(self):
        with captured_output():
            run_per_commit(['update', 'only'], None, 'main', self.ws, GitBackend(self.ws), sleep=None,
                           dry_run=False)
        self.assertEqual([name for name, *_ in self.ran], ['only', 'only'])

    def test_message_goes_to_the_first_step_only(self):
        self.run_steps(message='One\nTwo')
        self.assertEqual(
            [message for *_, message in self.ran], ['One\nTwo', None])

    def test_message_starting_with_a_dash(self):
        """argparse would read "-WIP" after -m as an option."""
        self.run_steps(message='-WIP')
        self.assertEqual([message for *_, message in self.ran], ['-WIP', None])

    def test_relative_base_means_the_same_commit_for_every_step(self):
        self.run_steps(base_branch='HEAD~2')
        main = self.git('rev-parse', 'main')
        self.assertEqual([base for _, _, base, _ in self.ran], [main, main])

    def test_sleeps_between_steps_but_not_after_the_last(self):
        self.run_steps(sleep=5)
        self.sleep.assert_called_once_with(5)

    def test_no_sleep_without_a_delay(self):
        self.run_steps()
        self.sleep.assert_not_called()

    def test_failing_first_step_resumes_from_the_base_with_the_message(self):
        self.errors['first'] = CommandError('p4 said no', returncode=3)
        error = self.step_fails(message='M')
        first = f"{self.git('rev-parse', '--short=7', 'HEAD~1')} First commit"
        main = self.git('rev-parse', 'main')
        self.assertEqual(str(error), f'Step 1 of 2 failed at {first}: p4 said no\n'
                         'After fixing the problem, resume with: '
                         f'git p4son update rest --per-commit --message=M -b {main}')
        self.assertEqual(error.returncode, 3)
        self.assertEqual([name for name, *_ in self.ran], ['first'])
        self.assert_at('feature', self.feature_head)

    def test_failing_first_step_resumes_with_the_message_file(self):
        self.errors['first'] = CommandError('no')
        error = self.step_fails(message='M', message_file='/tmp/msg.txt')
        main = self.git('rev-parse', 'main')
        self.assertTrue(str(error).endswith(
            f'git p4son update rest --per-commit -F /tmp/msg.txt -b {main}'), error)

    def test_failing_later_step_resumes_after_the_last_commit_that_worked(self):
        self.errors['rest'] = CommandError('no')
        error = self.step_fails(message='M')
        first = self.git('rev-parse', 'HEAD~1')
        self.assertTrue(str(error).endswith(
            f'resume with: git p4son update rest --per-commit -b {first}'), error)
        self.assert_at('feature', self.feature_head)

    def test_resumable_adds_a_note_or_stops_the_hint(self):
        self.errors['rest'] = CommandError('no')
        self.assertIn('\nCL 9 exists. After fixing the problem, resume with:',
                      str(self.step_fails(resumable=lambda: 'CL 9 exists.')))
        self.assertRegex(str(self.step_fails(resumable=lambda: None)),
                         r'^Step 2 of 2 failed at \w+ Second commit: no$')

    def test_dry_run_prints_the_plan_without_checking_out(self):
        out = self.run_steps(dry_run=True, message='Line one\nLine two')
        main = self.git('rev-parse', 'main')
        self.assertEqual(self.ran, [])
        for line in (f'First commit: git p4son update first -b {main}\n',
                     f'Second commit: git p4son update rest -b {main}\n'):
            self.assertIn(line, out)
        self.assertIn('Line one\nLine two', out)
        self.assert_at('feature', self.feature_head)

    def test_no_commits(self):
        with self.assertRaisesRegex(CommandError, 'No commits found since feature'):
            self.run_steps(base_branch='feature')

    def test_dirty_workspace(self):
        self.write_file('stray.txt')
        with self.assertRaisesRegex(CommandError, 'not clean'):
            self.run_steps()
        self.assertEqual(self.ran, [])

    def test_failing_return_keeps_the_step_error(self):
        start_patch(self, 'git_p4son.git_backend.switch_branch',
                    side_effect=RunError('git switch failed'))
        self.errors['first'] = CommandError('p4 said no')
        with captured_output() as out, self.assertRaisesRegex(CommandError, 'Step 1 of 2 failed at .*: p4 said no'):
            run_per_commit(['update', 'first'], ['update', 'rest'], 'main', self.ws, GitBackend(self.ws),
                           sleep=None, dry_run=False)
        self.assertIn('git switch failed', out.getvalue())

    def test_failing_return_after_success_raises(self):
        start_patch(self, 'git_p4son.git_backend.switch_branch',
                    side_effect=RunError('git switch failed'))
        with self.assertRaisesRegex(RunError, 'git switch failed'):
            self.run_steps()

    def test_returns_to_a_detached_start(self):
        self.git('switch', '-q', '--detach')
        self.run_steps()
        self.assert_at('', self.feature_head)


if __name__ == '__main__':
    unittest.main()
