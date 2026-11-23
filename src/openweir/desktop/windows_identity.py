"""Read a process's user SID from its token, never a username or an owner supplied by another
process."""

import ctypes
import sys
from ctypes import wintypes

_TOKEN_QUERY = 0x0008
_TOKEN_USER = 1  # TOKEN_INFORMATION_CLASS.TokenUser
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def current_sid() -> str:
    """This process's user SID (``S-1-5-21-…``); empty off Windows or when it cannot be read."""
    if sys.platform != "win32":
        return ""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    return _token_sid(kernel.GetCurrentProcess())


def process_sid(pid: int) -> str:
    """The user SID of process ``pid``; empty off Windows, when the process is gone, or when
    this user may not read its token, as for another user's process."""
    if sys.platform != "win32":
        return ""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    process = kernel.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return ""
    try:
        return _token_sid(process)
    finally:
        kernel.CloseHandle(process)


def _token_sid(process: int | None) -> str:
    """The user SID in the token of ``process``, a process handle."""
    if sys.platform != "win32":
        return ""
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel.LocalFree.argtypes = (ctypes.c_void_p,)
    security.OpenProcessToken.argtypes = (
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    )
    security.GetTokenInformation.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    security.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR))
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(process, _TOKEN_QUERY, ctypes.byref(token)):
        return ""
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, _TOKEN_USER, None, 0, ctypes.byref(size))
        if not size.value:
            return ""
        buffer = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, _TOKEN_USER, buffer, size, ctypes.byref(size)):
            return ""
        sid_pointer = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not security.ConvertSidToStringSidW(sid_pointer, ctypes.byref(text)):
            return ""
        try:
            return text.value or ""
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)
