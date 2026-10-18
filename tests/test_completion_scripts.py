"""Tests for the jj p4son completion hooks in git_p4son/completions, run in each shell against jj's own completion."""

import os
import shlex
import subprocess
import sys
import unittest
from importlib.resources import files

from tests.helpers import isolate_jj, requires_jj, requires_shell, WorkspaceTestCase

COMPLETIONS = str(files('git_p4son') / 'completions')
CANDIDATES = ['sync\tSync the git repository with Perforce',
              'sync-split-users\tShow or edit the split users']


class HookTestCase(WorkspaceTestCase):
    """A stub git-p4son on PATH that logs the words it is asked to complete and prints CANDIDATES."""

    def setUp(self):
        super().setUp()
        isolate_jj(self, self.ws)
        self.bin = os.path.join(self.ws, 'bin')
        os.makedirs(self.bin)
        self.log = os.path.join(self.ws, 'stub.log')
        candidates = self.write_file('candidates.txt', ''.join(
            f'{line}\n' for line in CANDIDATES))
        if sys.platform == 'win32':
            self.write_file(os.path.join('bin', 'git-p4son.cmd'),
                            f'@echo off\r\necho %*>>"{self.log}"\r\ntype "{candidates}"\r\n')
        else:
            stub = self.write_file(os.path.join('bin', 'git-p4son'),
                                   f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> {shlex.quote(self.log)}\n'
                                   f'cat {shlex.quote(candidates)}\n')
            os.chmod(stub, 0o755)
        self.env = {**os.environ, 'PATH': self.bin +
                    os.pathsep + os.environ['PATH']}

    def run_shell(self, command):
        """Run command, a list, in the workspace with the stub first on PATH; returns its output lines."""
        result = subprocess.run(command, cwd=self.ws,
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def asked(self):
        """The words the stub was asked to complete, one line per call."""
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [line.rstrip() for line in f]


@unittest.skipIf(sys.platform == 'win32', 'the bash hook is tested on POSIX')
@requires_shell('bash')
@requires_jj
class TestBashHook(HookTestCase):
    SETUPS = {'dynamic': 'eval "$(COMPLETE=bash jj)"',
              'static': 'eval "$(jj util completion bash)"'}

    def complete(self, *words, setup='dynamic', sourced=1):
        """Complete words, the last under the cursor, through jj's registered completer; returns COMPREPLY."""
        hook = shlex.quote(os.path.join(COMPLETIONS, 'git-p4son-jj.bash'))
        script = '\n'.join([
            self.SETUPS[setup], *[f'source {hook}'] * sourced,
            f'COMP_WORDS=({" ".join(shlex.quote(word) for word in words)})',
            f'COMP_CWORD={len(words) - 1}',
            'completer=$(complete -p jj | sed -E "s/.* -F ([^ ]+) .*/\\1/")',
            '"$completer" jj "${COMP_WORDS[COMP_CWORD]}" "${COMP_WORDS[COMP_CWORD-1]}"',
            'printf "%s\\n" "${COMPREPLY[@]}"'])
        return self.run_shell(['bash', '-c', script])

    def test_completes_the_words_after_p4son_through_git_p4son(self):
        self.assertEqual(self.complete('jj', 'p4son', 'sy'),
                         ['sync', 'sync-split-users'])
        self.assertEqual(self.asked(), ['complete -- sy'])

    def test_skips_jj_global_options(self):
        self.complete('jj', '--color', 'never',
                      '--ignore-working-copy', '-R', '.', 'p4son', 'sync', '-')
        self.complete('jj', '--config', '=', 'ui.color=never', 'p4son', 'sy')
        self.assertEqual(
            self.asked(), ['complete -- sync -', 'complete -- sy'])

    def test_hands_other_words_to_jj(self):
        for setup in self.SETUPS:
            with self.subTest(setup=setup):
                self.assertIn('log', self.complete('jj', 'lo', setup=setup))
        self.assertEqual(self.asked(), [])

    def test_sourced_twice_still_hands_over(self):
        self.assertIn('log', self.complete('jj', 'lo', sourced=2))
        self.assertEqual(self.complete('jj', 'p4son', 'sy', sourced=2), [
                         'sync', 'sync-split-users'])


@unittest.skipIf(sys.platform == 'win32', 'the zsh hook is tested on POSIX')
@requires_shell('zsh')
@requires_jj
class TestZshHook(HookTestCase):
    SETUPS = {'dynamic': 'eval "$(COMPLETE=zsh jj)"',
              'static': 'eval "$(jj util completion zsh)"'}

    def zsh(self, *lines):
        return self.run_shell(['zsh', '-f', '-c', '\n'.join(lines)])

    def hook(self):
        return f'source {shlex.quote(os.path.join(COMPLETIONS, "git-p4son-jj.zsh"))}'

    def complete(self, *words):
        """Complete words through the hook, with compdef and _describe stubbed and a stub jj completer."""
        return self.zsh(
            'typeset -A _comps',
            'compdef() { _comps[$2]=$1 }',
            '_describe() { print -rl -- "${(@P)2}" }',
            '_jj_stub() { print -r -- "jj: $words" }',
            '_comps[jj]=_jj_stub',
            self.hook(),
            f'words=({" ".join(shlex.quote(word) for word in words)})',
            f'CURRENT={len(words)}',
            '$_comps[jj]')

    def test_completes_the_words_after_p4son_through_git_p4son(self):
        self.assertEqual(self.complete('jj', 'p4son', 'sy'), [
                         line.replace('\t', ':') for line in CANDIDATES])
        self.assertEqual(self.asked(), ['complete -- sy'])

    def test_skips_jj_global_options(self):
        self.complete('jj', '--color', 'never',
                      '--config=ui.color=never', '-R', '.', 'p4son', 'sync', '-')
        self.assertEqual(self.asked(), ['complete -- sync -'])

    def test_hands_other_words_to_jj(self):
        self.assertEqual(self.complete('jj', 'lo'), ['jj: jj lo'])
        self.assertEqual(self.complete('jj', 'p4son'), ['jj: jj p4son'])
        self.assertEqual(self.asked(), [])

    def test_registers_over_jj_s_own_completer_once(self):
        for setup, original in (('dynamic', '_clap_dynamic_completer_jj'), ('static', '_jj')):
            with self.subTest(setup=setup):
                self.assertEqual(self.zsh(
                    'autoload -Uz compinit && compinit -u -D', self.SETUPS[setup], self.hook(
                    ), self.hook(),
                    'print -r -- "$_comps[jj] $_git_p4son_jj_original"'), [f'_git_p4son_jj {original}'])


@requires_shell('pwsh')
@requires_jj
class TestPowerShellHook(HookTestCase):
    SETUPS = {'dynamic': '$env:COMPLETE = "powershell"; (& jj) | Out-String | Invoke-Expression; '
                         'Remove-Item Env:\\COMPLETE',
              'static': '(& jj util completion power-shell) | Out-String | Invoke-Expression'}

    def complete(self, line, setup='dynamic', sourced=1, then=()):
        """Complete line, with the cursor at its end, through PowerShell's own completion; returns the matches.

        then holds lines to run after the setup, before completing."""
        hook = os.path.join(COMPLETIONS, 'git-p4son-jj.ps1')
        script = os.path.join(self.ws, 'complete.ps1')
        self.write_file('complete.ps1', '\n'.join([
            self.SETUPS[setup], *[f". '{hook}'"] * sourced, *then,
            f"$line = '{line}'",
            '(TabExpansion2 -inputScript $line -cursorColumn $line.Length).CompletionMatches | '
            'ForEach-Object { $_.CompletionText }']))
        return self.run_shell(['pwsh', '-NoProfile', '-NonInteractive', '-File', script])

    def test_completes_the_words_after_p4son_through_git_p4son(self):
        self.assertEqual(self.complete('jj p4son sy'),
                         ['sync', 'sync-split-users'])
        self.assertEqual(self.asked(), ['complete -- sy'])

    def test_skips_jj_global_options(self):
        self.complete(
            'jj --color never --config=ui.color=never -R . p4son sync -')
        self.assertEqual(self.asked(), ['complete -- sync -'])

    def test_hands_other_words_to_jj(self):
        for setup in self.SETUPS:
            with self.subTest(setup=setup):
                self.assertIn('log', self.complete('jj lo', setup=setup))
        self.complete('jj p4son')
        self.assertEqual(self.asked(), [])

    def test_sourced_twice_still_hands_over(self):
        self.assertIn('log', self.complete('jj lo', sourced=2))
        self.assertEqual(self.complete('jj p4son sy', sourced=2), [
                         'sync', 'sync-split-users'])

    def test_an_unreadable_completer_table_leaves_jj_s_completer_in_place(self):
        """Should PowerShell's internals change, the hook warns and stays out, rather than leave jj without one."""
        out = self.complete(
            'jj lo', then=[self.SETUPS['dynamic'], '_GitP4sonHookJj $null 3>&1'])
        self.assertIn('log', out)
        self.assertTrue(
            any('jj p4son completion is not set up' in line for line in out), out)


if __name__ == '__main__':
    unittest.main()
