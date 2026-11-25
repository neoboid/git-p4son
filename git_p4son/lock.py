"""File locks that refuse when busy, and the mark of a lock a caller holds."""

import contextlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path

from .common import CommandError

# Names the workspace that this process, or the git-p4son that started it, holds its lock on.
_LOCK_ENV = '_GIT_P4SON_LOCK'


def _root(workspace_dir: str) -> str:
    return str(Path(workspace_dir).resolve())


def locked_by_caller(workspace_dir: str) -> bool:
    """Whether the git-p4son that started this process holds its lock on the workspace."""
    return os.environ.get(_LOCK_ENV) == _root(workspace_dir)


@contextlib.contextmanager
def marked_as_locked(workspace_dir: str) -> Iterator[None]:
    """Mark the workspace as under git-p4son's lock for the processes it starts, as hooks."""
    inherited = os.environ.get(_LOCK_ENV)
    os.environ[_LOCK_ENV] = _root(workspace_dir)
    try:
        yield
    finally:
        if inherited is None:
            os.environ.pop(_LOCK_ENV, None)
        else:
            os.environ[_LOCK_ENV] = inherited


# A lock on the file at path, raising CommandError(busy) when it is held. It is taken the way jj takes its
# working-copy lock, and defined per platform, so a type checker checks only the one it targets.
if sys.platform == 'win32':
    @contextlib.contextmanager
    def file_lock(path: Path, busy: str) -> Iterator[None]:
        import ctypes
        from ctypes import wintypes

        class Overlapped(ctypes.Structure):
            _fields_ = [('Internal', ctypes.c_size_t),
                        ('InternalHigh', ctypes.c_size_t),
                        ('Offset', wintypes.DWORD),
                        ('OffsetHigh', wintypes.DWORD),
                        ('hEvent', wintypes.HANDLE)]

        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD,
                                       wintypes.DWORD, ctypes.c_void_p,
                                       wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                      wintypes.DWORD, wintypes.DWORD,
                                      wintypes.DWORD, ctypes.POINTER(Overlapped)]
        kernel.LockFileEx.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL

        # Share reads/writes, not deletion, matching jj's LockFileEx protocol.
        handle = kernel.CreateFileW(str(path), 0x40000000, 3, None, 4, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        locked = False
        try:
            overlapped = Overlapped()
            locked = bool(kernel.LockFileEx(handle, 3, 0, 0xffffffff,
                                            0xffffffff, ctypes.byref(overlapped)))
            if not locked:
                error = ctypes.get_last_error()
                if error == 33:
                    raise CommandError(busy)
                raise ctypes.WinError(error)
            yield
        finally:
            kernel.CloseHandle(handle)
            if locked:
                try:
                    path.unlink(missing_ok=True)
                except PermissionError:
                    pass
else:
    @contextlib.contextmanager
    def file_lock(path: Path, busy: str) -> Iterator[None]:
        import fcntl

        while True:
            file = path.open('wb')
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
                # jj unlinks the file before unlocking; waiters must lock the new inode.
                if os.fstat(file.fileno()).st_nlink:
                    break
            except BlockingIOError as error:
                file.close()
                raise CommandError(busy) from error
            except BaseException:
                file.close()
                raise
            file.close()
        try:
            yield
        finally:
            try:
                path.unlink(missing_ok=True)
            finally:
                file.close()
