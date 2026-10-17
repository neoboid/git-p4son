"""Working-copy locking compatible with jj's local working copy."""

import contextlib
import os
import sys
from pathlib import Path

from .common import CommandError


@contextlib.contextmanager
def working_copy_lock(workspace_dir: str):
    """Hold jj's working-copy lock, raising CommandError if it is busy."""
    path = Path(workspace_dir) / '.jj' / 'working_copy' / 'working_copy.lock'
    with _lock(path):
        yield


# Defined per platform, so a type checker checks only the one it targets.
if sys.platform == 'win32':
    @contextlib.contextmanager
    def _lock(path: Path):
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
                    raise CommandError(
                        'The jj working copy is busy. Run the command again when jj finishes.')
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
    def _lock(path: Path):
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
                raise CommandError(
                    'The jj working copy is busy. Run the command again when jj finishes.') from error
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
