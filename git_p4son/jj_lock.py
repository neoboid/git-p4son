"""Working-copy locking compatible with jj's local working copy."""

import contextlib
from collections.abc import Iterator
from pathlib import Path

from .lock import file_lock


@contextlib.contextmanager
def working_copy_lock(workspace_dir: str) -> Iterator[None]:
    """Hold jj's working-copy lock, raising CommandError if it is busy."""
    path = Path(workspace_dir) / '.jj' / 'working_copy' / 'working_copy.lock'
    with file_lock(path, 'The jj working copy is busy. Run the command again when jj finishes.'):
        yield
