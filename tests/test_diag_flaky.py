"""Throwaway: repeat the flaky TestJjSync scenario and print the state of b.txt when sync refuses it."""

import os

from git_p4son.common import CommandError
from tests.test_jj import JjP4RepoTestCase


class TestDiagFlaky(JjP4RepoTestCase):
    def build_workspace(self):
        super().build_workspace()
        self.jj('new', 'main')

    def state(self, label):
        ws = self.ws
        st = os.stat(os.path.join(ws, 'b.txt'))
        lines = [f'--- {label}',
                 f'disk b.txt: {open(os.path.join(ws, "b.txt"), "rb").read()!r} size={st.st_size} '
                 f'mtime_ns={st.st_mtime_ns} ctime_ns={st.st_ctime_ns}']
        for name in ('.git/index', '.jj/working_copy/tree_state'):
            path = os.path.join(ws, *name.split('/'))
            if os.path.exists(path):
                lines.append(f'{name}: mtime_ns={os.stat(path).st_mtime_ns}')
        working = self.jj('--ignore-working-copy', 'log', '-r',
                          '@', '--no-graph', '-T', 'commit_id')
        parent = self.jj('--ignore-working-copy', 'log', '-r',
                         '@-', '--no-graph', '-T', 'commit_id')
        lines.append(
            f'@ {working}: b.txt={self.git("show", f"{working}:b.txt")!r}')
        lines.append(
            f'@- {parent}: b.txt={self.git("show", f"{parent}:b.txt")!r}')
        lines.append(f'git HEAD: {self.git("rev-parse", "HEAD")}')
        lines.append('git index entry:\n' +
                     self.git('ls-files', '--debug', '--stage', 'b.txt'))
        lines.append('jj file state:\n' + '\n'.join(
            line for line in self.jj('--ignore-working-copy', 'debug', 'local-working-copy').splitlines()
            if 'b.txt' in line or 'mtime' in line.lower() and 'b.txt' in line))
        return '\n'.join(lines)

    def scenario(self):
        self.jj('describe', '-m', 'work in progress')
        before = self.state('after describe, before edit')
        self.edit('b.txt', 'b2\n')
        after = self.state('after edit, before sync')
        self.p4.submit(101, {'c.txt': 'c1\n'})
        try:
            self.p4son('sync')
        except CommandError as error:
            error.add_note(
                '\n'.join([before, after, self.state('after the refused sync')]))
            raise


for i in range(400):
    setattr(TestDiagFlaky, f'test_{i:03}', TestDiagFlaky.scenario)
