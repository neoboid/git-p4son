"""Perforce client spec abstraction."""

from dataclasses import dataclass

from .common import run


def parse_ztag_output(lines: list[str]) -> dict[str, str]:
    """Parse p4 -ztag output into a dict."""
    fields = {}
    for line in lines:
        if line.startswith('... '):
            parts = line[4:].split(' ', 1)
            key = parts[0]
            value = parts[1] if len(parts) > 1 else ''
            fields[key] = value
    return fields


@dataclass
class P4ClientSpec:
    """Parsed Perforce client specification."""

    name: str
    root: str
    options: list[str]
    stream: str | None

    @property
    def clobber(self) -> bool:
        return 'clobber' in self.options


def get_client_spec(cwd: str) -> P4ClientSpec | None:
    """Get the client spec, or None if not in a valid workspace.

    Uses the presence of 'Update' in the ztag output to distinguish a real
    workspace from the default spec p4 returns when CWD is outside any workspace.
    """
    result = run(['p4', '-ztag', 'client', '-o'], cwd=cwd)
    fields = parse_ztag_output(result.stdout)
    if 'Update' not in fields:
        return None
    return P4ClientSpec(
        name=fields['Client'],
        root=fields['Root'],
        options=fields['Options'].split(),
        stream=fields.get('Stream'),
    )
