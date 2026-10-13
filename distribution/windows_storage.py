"""Current-user-only Windows app storage (native acceptance still required).

Uses the operating system DACL API, never icacls shell text or profile changes.
No credential is written before the directory owner and every ACE are checked.
"""
from __future__ import annotations

import contextlib
import ctypes
from ctypes import wintypes
from pathlib import Path
import os


class StorageError(ValueError):
    pass


def _api():
    if os.name != "nt":
        raise StorageError("Windows storage API requires a native Windows host")
    adv = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    adv.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    adv.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    adv.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
    adv.SetFileSecurityW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p]
    adv.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p)]
    adv.GetAclInformation.argtypes = [ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.c_int]
    adv.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    adv.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    adv.GetSecurityDescriptorControl.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.WORD), ctypes.POINTER(wintypes.DWORD)]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    return adv, kernel


@contextlib.contextmanager
def _identity(adv, kernel):
    token = wintypes.HANDLE()
    if not adv.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise StorageError("Cannot inspect the current Windows user identity")
    try:
        needed = wintypes.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(needed))
        buffer = ctypes.create_string_buffer(needed.value)
        if not adv.GetTokenInformation(token, 1, buffer, len(buffer), ctypes.byref(needed)):
            raise StorageError("Cannot inspect the current Windows token")
        sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        yield sid
    finally:
        kernel.CloseHandle(token)


def verify_private(path: Path, *, directory: bool):
    adv, kernel = _api()
    owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    if adv.GetNamedSecurityInfoW(str(path), 1, 5, ctypes.byref(owner), None, ctypes.byref(acl), None, ctypes.byref(descriptor)):
        raise StorageError("Cannot verify private Windows app storage")
    try:
        with _identity(adv, kernel) as sid:
            if not owner or not acl or not adv.EqualSid(owner, sid):
                raise StorageError("Windows app storage has an unexpected owner or null DACL")
            control, revision = wintypes.WORD(), wintypes.DWORD()
            if not adv.GetSecurityDescriptorControl(descriptor, ctypes.byref(control), ctypes.byref(revision)):
                raise StorageError("Cannot inspect Windows app storage protection")
            if directory and not control.value & 0x1000:
                raise StorageError("Windows app storage must block inherited permissions")
            class AclSize(ctypes.Structure):
                _fields_ = [("count", wintypes.DWORD), ("used", wintypes.DWORD), ("free", wintypes.DWORD)]
            info = AclSize()
            if not adv.GetAclInformation(acl, ctypes.byref(info), ctypes.sizeof(info), 2) or info.count != 1:
                raise StorageError("Windows app storage must grant access only to this user")
            ace = ctypes.c_void_p()
            if not adv.GetAce(acl, 0, ctypes.byref(ace)):
                raise StorageError("Cannot inspect Windows app storage access")
            # ACCESS_ALLOWED_ACE: header(4), access mask(4), SID starts at byte 8.
            if ctypes.c_ubyte.from_address(ace.value).value != 0 or not adv.EqualSid(ace.value + 8, sid):
                raise StorageError("Windows app storage grants access to another identity")
            if ctypes.c_uint32.from_address(ace.value + 4).value != 0x1F01FF:
                raise StorageError("Windows app storage has unexpected owner access")
    finally:
        kernel.LocalFree(descriptor)


def private_directory(path: Path):
    if path.is_symlink() or (path.exists() and getattr(path, "is_junction", lambda: False)()):
        raise StorageError("Windows app storage must not be redirected")
    created = False
    try:
        path.mkdir(parents=True, exist_ok=False)
        created = True
    except FileExistsError:
        pass
    if created:
        adv, kernel = _api()
        with _identity(adv, kernel) as sid:
            text = wintypes.LPWSTR()
            if not adv.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                raise StorageError("Cannot encode Windows app storage identity")
            descriptor = ctypes.c_void_p()
            try:
                sddl = "D:P(A;OICI;FA;;;" + text.value + ")"
                if not adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(sddl, 1, ctypes.byref(descriptor), None):
                    raise StorageError("Cannot create private Windows storage permissions")
                if not adv.SetFileSecurityW(str(path), 0x80000004, descriptor):
                    raise StorageError("Cannot protect Windows app storage")
            finally:
                kernel.LocalFree(text)
                if descriptor:
                    kernel.LocalFree(descriptor)
    # Existing directories are never repaired or expanded automatically.
    verify_private(path, directory=True)
    return path.resolve()
