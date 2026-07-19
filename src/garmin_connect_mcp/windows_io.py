"""Windows file primitives needed for atomic token replacement."""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path

if os.name == "nt":
    import msvcrt

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _GENERIC_READ = 0x80000000
    _FILE_SHARE_READ = 0x00000001
    _FILE_SHARE_WRITE = 0x00000002
    _FILE_SHARE_DELETE = 0x00000004
    _OPEN_EXISTING = 3
    _FILE_ATTRIBUTE_NORMAL = 0x00000080
    _REPLACEFILE_WRITE_THROUGH = 0x00000001
    _MOVEFILE_REPLACE_EXISTING = 0x00000001
    _MOVEFILE_WRITE_THROUGH = 0x00000008
    _INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    _kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.ReplaceFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _kernel32.ReplaceFileW.restype = wintypes.BOOL
    _kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    _kernel32.MoveFileExW.restype = wintypes.BOOL


def open_shared_read(path: Path) -> int:
    """Open a descriptor that does not block another process's atomic replace."""
    if os.name != "nt":
        return os.open(path, os.O_RDONLY)

    handle = _kernel32.CreateFileW(
        str(path),
        _GENERIC_READ,
        _FILE_SHARE_READ | _FILE_SHARE_WRITE | _FILE_SHARE_DELETE,
        None,
        _OPEN_EXISTING,
        _FILE_ATTRIBUTE_NORMAL,
        None,
    )
    if handle == _INVALID_HANDLE_VALUE:
        error = ctypes.get_last_error()
        raise OSError(error, f"Could not open shared token reader: {ctypes.FormatError(error)}")
    try:
        descriptor = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except Exception:
        _kernel32.CloseHandle(handle)
        raise
    return descriptor


def atomic_replace(source: Path, target: Path) -> None:
    """Replace a Windows file even while readers allow shared deletion."""
    if os.name != "nt":
        os.replace(source, target)
        return

    if os.path.lexists(target):
        if not _kernel32.ReplaceFileW(
            str(target),
            str(source),
            None,
            _REPLACEFILE_WRITE_THROUGH,
            None,
            None,
        ):
            error = ctypes.get_last_error()
            raise OSError(error, f"Atomic ReplaceFileW failed: {ctypes.FormatError(error)}")
        return

    if not _kernel32.MoveFileExW(
        str(source),
        str(target),
        _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH,
    ):
        error = ctypes.get_last_error()
        raise OSError(error, f"Atomic MoveFileExW failed: {ctypes.FormatError(error)}")
