"""Minimal Windows ACL adapter for owner-only token storage."""

from __future__ import annotations

import ctypes
import os
import re
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


class WindowsAclError(OSError):
    """Raised when a Windows ACL cannot be inspected or hardened."""


@dataclass(frozen=True)
class WindowsAclDescriptor:
    """Serializable owner and DACL metadata for atomic file replacement."""

    owner_sid: str
    dacl: str


_TRUSTED_SYSTEM_SIDS = {
    "S-1-5-18",
    "SY",
    "S-1-5-32-544",
    "BA",
    # Windows Modules Installer / TrustedInstaller.
    "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464",
}
_DIRECTORY_MUTATION_MASK = (
    0x00000002  # FILE_ADD_FILE
    | 0x00000004  # FILE_ADD_SUBDIRECTORY
    | 0x00000040  # FILE_DELETE_CHILD
    | 0x00010000  # DELETE
    | 0x00040000  # WRITE_DAC
    | 0x00080000  # WRITE_OWNER
    | 0x10000000  # GENERIC_ALL
    | 0x40000000  # GENERIC_WRITE
)
_DANGEROUS_SYMBOLIC_RIGHTS = {
    "GA",
    "GW",
    "FA",
    "FW",
    "SD",
    "WD",
    "WO",
    "CC",
    "DC",
    "LC",
    "DT",
}
_READ_ONLY_SYMBOLIC_RIGHTS = {"GR", "GX", "FR", "FX", "RC"}


if os.name == "nt":
    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _SE_FILE_OBJECT = 1
    _OWNER_SECURITY_INFORMATION = 0x00000001
    _DACL_SECURITY_INFORMATION = 0x00000004
    _PROTECTED_DACL_SECURITY_INFORMATION = 0x80000000
    _UNPROTECTED_DACL_SECURITY_INFORMATION = 0x20000000
    _SDDL_REVISION_1 = 1
    _TOKEN_QUERY = 0x0008
    _TOKEN_USER = 1

    class _SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class _TokenUser(ctypes.Structure):
        _fields_ = [("user", _SidAndAttributes)]

    _advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_wchar_p),
    ]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_wchar_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.ULONG),
    ]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    _advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    _advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
    ]
    _advapi32.SetNamedSecurityInfoW.restype = wintypes.DWORD
    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    _kernel32.LocalFree.restype = ctypes.c_void_p


def acl_is_owner_only(path: Path) -> bool:
    """Return whether only the current user and SYSTEM have allow ACEs."""
    _require_windows()
    try:
        owner_sid, dacl = _read_security_descriptor(path)
        current_sid = _current_user_sid()
    except WindowsAclError:
        return False

    if owner_sid != current_sid or not dacl.startswith("D:P"):
        return False

    allowed_sids = {current_sid, "S-1-5-18", "SY"}
    current_user_has_full_access = False
    for raw_ace in re.findall(r"\(([^()]*)\)", dacl):
        fields = raw_ace.split(";")
        if len(fields) != 6:
            return False
        ace_type, _flags, rights, _object_guid, _inherit_guid, sid = fields
        # Keep the accepted descriptor deliberately small. Object/callback
        # allow ACEs can also grant access, and unfamiliar ACE forms are not
        # safe to classify as owner-only.
        if ace_type != "A":
            return False
        if sid not in allowed_sids:
            return False
        if sid == current_sid and rights == "FA":
            current_user_has_full_access = True
    return current_user_has_full_access


def enforce_owner_only_acl(path: Path, *, directory: bool) -> None:
    """Install a protected DACL for the current user and local SYSTEM."""
    _require_windows()
    current_sid = _current_user_sid()
    owner_sid, _dacl = _read_security_descriptor(path)
    if owner_sid != current_sid:
        raise WindowsAclError(f"Refusing to change ACL on a path owned by another SID: {path}")

    inheritance = "OICI" if directory else ""
    sddl = f"D:P(A;{inheritance};FA;;;{current_sid})(A;{inheritance};FA;;;SY)"
    _apply_dacl(path, WindowsAclDescriptor(current_sid, sddl))

    if not acl_is_owner_only(path):
        raise WindowsAclError(f"Owner-only ACL verification failed after hardening: {path}")


def read_acl_descriptor(path: Path) -> WindowsAclDescriptor:
    """Capture the exact owner SID and DACL for later atomic replacement."""
    _require_windows()
    owner_sid, dacl = _read_security_descriptor(path)
    return WindowsAclDescriptor(owner_sid, dacl)


def current_user_sid() -> str:
    """Return the SID used for ownership checks."""
    _require_windows()
    return _current_user_sid()


def apply_acl_descriptor(path: Path, descriptor: WindowsAclDescriptor) -> None:
    """Apply and verify previously captured Windows file protection."""
    _require_windows()
    if descriptor.owner_sid != _current_user_sid():
        raise WindowsAclError(
            f"Refusing to restore an ACL owned by another SID: {descriptor.owner_sid}"
        )
    current_owner, _current_dacl = _read_security_descriptor(path)
    if current_owner != descriptor.owner_sid:
        raise WindowsAclError(f"Replacement path has an unexpected owner SID: {path}")
    _apply_dacl(path, descriptor)
    if read_acl_descriptor(path) != descriptor:
        raise WindowsAclError(f"Windows ACL verification failed after replacement: {path}")


def directory_entry_integrity_is_protected(
    path: Path,
    *,
    require_current_owner: bool,
) -> bool:
    """Conservatively reject parents another SID can add to, delete from, or re-ACL."""
    _require_windows()
    try:
        descriptor = read_acl_descriptor(path)
        current_sid = _current_user_sid()
    except WindowsAclError:
        return False
    trusted_sids = {current_sid, "OW", *_TRUSTED_SYSTEM_SIDS}
    if "NO_ACCESS_CONTROL" in descriptor.dacl or not descriptor.dacl.startswith("D:"):
        return False
    if require_current_owner:
        if descriptor.owner_sid != current_sid:
            return False
    elif descriptor.owner_sid not in trusted_sids:
        return False

    for raw_ace in re.findall(r"\(([^()]*)\)", descriptor.dacl):
        fields = raw_ace.split(";")
        if len(fields) != 6:
            return False
        ace_type, flags, rights, _object_guid, _inherit_guid, sid = fields
        if "IO" in flags or sid in trusted_sids:
            continue
        if ace_type in {"A", "OA", "XA", "ZA"} and _rights_allow_directory_mutation(rights):
            return False
    return True


def file_integrity_is_protected(path: Path) -> bool:
    """Reject a file another SID can write, delete, or re-ACL during a CAS operation."""
    _require_windows()
    try:
        descriptor = read_acl_descriptor(path)
        current_sid = _current_user_sid()
    except WindowsAclError:
        return False
    trusted_sids = {current_sid, "OW", *_TRUSTED_SYSTEM_SIDS}
    if (
        descriptor.owner_sid != current_sid
        or "NO_ACCESS_CONTROL" in descriptor.dacl
        or not descriptor.dacl.startswith("D:")
    ):
        return False
    for raw_ace in re.findall(r"\(([^()]*)\)", descriptor.dacl):
        fields = raw_ace.split(";")
        if len(fields) != 6:
            return False
        ace_type, flags, rights, _object_guid, _inherit_guid, sid = fields
        if "IO" in flags or sid in trusted_sids:
            continue
        if ace_type in {"A", "OA", "XA", "ZA"} and _rights_allow_file_mutation(rights):
            return False
    return True


def _rights_allow_directory_mutation(rights: str) -> bool:
    if rights.startswith("0x"):
        try:
            return bool(int(rights, 16) & _DIRECTORY_MUTATION_MASK)
        except ValueError:
            return True
    if len(rights) % 2:
        return True
    tokens = {rights[index : index + 2] for index in range(0, len(rights), 2)}
    if tokens & _DANGEROUS_SYMBOLIC_RIGHTS:
        return True
    return not tokens.issubset(_READ_ONLY_SYMBOLIC_RIGHTS)


def _rights_allow_file_mutation(rights: str) -> bool:
    if rights.startswith("0x"):
        try:
            return bool(int(rights, 16) & (_DIRECTORY_MUTATION_MASK | 0x00010000))
        except ValueError:
            return True
    return _rights_allow_directory_mutation(rights)


def _apply_dacl(path: Path, descriptor: WindowsAclDescriptor) -> None:
    security_descriptor = ctypes.c_void_p()
    if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        descriptor.dacl,
        _SDDL_REVISION_1,
        ctypes.byref(security_descriptor),
        None,
    ):
        _raise_last_error("Could not create the protected token-store DACL")

    try:
        dacl_present = wintypes.BOOL()
        dacl = ctypes.c_void_p()
        dacl_defaulted = wintypes.BOOL()
        if not _advapi32.GetSecurityDescriptorDacl(
            security_descriptor,
            ctypes.byref(dacl_present),
            ctypes.byref(dacl),
            ctypes.byref(dacl_defaulted),
        ):
            _raise_last_error("Could not read the generated token-store DACL")
        if not dacl_present:
            raise WindowsAclError("Generated token-store security descriptor has no DACL")

        protection_flag = (
            _PROTECTED_DACL_SECURITY_INFORMATION
            if descriptor.dacl.startswith("D:P")
            else _UNPROTECTED_DACL_SECURITY_INFORMATION
        )
        result = _advapi32.SetNamedSecurityInfoW(
            str(path),
            _SE_FILE_OBJECT,
            _DACL_SECURITY_INFORMATION | protection_flag,
            None,
            None,
            dacl,
            None,
        )
        if result:
            raise WindowsAclError(f"Could not harden ACL for {path}: Windows error {result}")
    finally:
        _kernel32.LocalFree(security_descriptor)


def _current_user_sid() -> str:
    token = wintypes.HANDLE()
    if not _advapi32.OpenProcessToken(
        _kernel32.GetCurrentProcess(),
        _TOKEN_QUERY,
        ctypes.byref(token),
    ):
        _raise_last_error("Could not open the current process token")

    try:
        size = wintypes.DWORD()
        _advapi32.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        if not size.value:
            _raise_last_error("Could not determine the current user SID size")
        buffer = ctypes.create_string_buffer(size.value)
        if not _advapi32.GetTokenInformation(
            token,
            _TOKEN_USER,
            buffer,
            size,
            ctypes.byref(size),
        ):
            _raise_last_error("Could not read the current user SID")
        token_user = ctypes.cast(buffer, ctypes.POINTER(_TokenUser)).contents
        return _sid_to_string(token_user.user.sid)
    finally:
        _kernel32.CloseHandle(token)


def _read_security_descriptor(path: Path) -> tuple[str, str]:
    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    security_descriptor = ctypes.c_void_p()
    result = _advapi32.GetNamedSecurityInfoW(
        str(path),
        _SE_FILE_OBJECT,
        _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(security_descriptor),
    )
    if result:
        raise WindowsAclError(f"Could not inspect ACL for {path}: Windows error {result}")

    try:
        owner_sid = _sid_to_string(owner)
        string_descriptor = ctypes.c_wchar_p()
        if not _advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            security_descriptor,
            _SDDL_REVISION_1,
            _DACL_SECURITY_INFORMATION,
            ctypes.byref(string_descriptor),
            None,
        ):
            _raise_last_error(f"Could not serialize ACL for {path}")
        try:
            return owner_sid, _normalize_dacl(string_descriptor.value or "")
        finally:
            _kernel32.LocalFree(string_descriptor)
    finally:
        _kernel32.LocalFree(security_descriptor)


def _sid_to_string(sid: ctypes.c_void_p) -> str:
    string_sid = ctypes.c_wchar_p()
    if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(string_sid)):
        _raise_last_error("Could not serialize a Windows SID")
    try:
        return string_sid.value or ""
    finally:
        _kernel32.LocalFree(string_sid)


def _normalize_dacl(dacl: str) -> str:
    """Ignore auto-inheritance bookkeeping while preserving effective ACLs."""
    if not dacl.startswith("D:") or "(" not in dacl:
        return dacl
    header, aces = dacl.split("(", 1)
    flags = header.removeprefix("D:")
    protected = "P" if "P" in flags else ""
    return f"D:{protected}({aces}"


def _raise_last_error(message: str) -> None:
    error = ctypes.get_last_error()
    raise WindowsAclError(f"{message}: {ctypes.FormatError(error)}")


def _require_windows() -> None:
    if os.name != "nt":
        raise WindowsAclError("Windows ACL operations are unavailable on this platform")
