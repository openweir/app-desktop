"""macOS: messages to Objective-C objects through ctypes, without PyObjC. The title bar, the Dock,
the session's quit reason and the login item use it; it works the same in a development run and
in OpenWeir.app."""

from __future__ import annotations

import ctypes
from collections.abc import Iterator
from contextlib import contextmanager
from functools import cache
from typing import Any

LIBOBJC = "/usr/lib/libobjc.A.dylib"


@cache
def _runtime() -> ctypes.CDLL:
    """The Objective-C runtime, loaded once; OSError where there is none."""
    runtime = ctypes.CDLL(LIBOBJC)
    for name in ("objc_getClass", "sel_registerName"):
        function = getattr(runtime, name)
        function.restype, function.argtypes = ctypes.c_void_p, [ctypes.c_char_p]
    runtime.objc_autoreleasePoolPush.restype = ctypes.c_void_p
    runtime.objc_autoreleasePoolPop.argtypes = [ctypes.c_void_p]
    return runtime


class ObjC:
    """objc_msgSend and an autorelease pool; raises OSError off macOS."""

    def __init__(self) -> None:
        self._objc = _runtime()
        self._send_address = ctypes.cast(self._objc.objc_msgSend, ctypes.c_void_p).value or 0

    def send(
        self, restype: Any, receiver: int | None, selector: bytes, *args: Any, argtypes: tuple = ()
    ) -> Any:
        """objc_msgSend, cast to the method's own signature (arm64 needs the exact one)."""
        function = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p, *argtypes)(
            self._send_address
        )
        return function(receiver, self._objc.sel_registerName(selector), *args)

    def cls(self, name: bytes) -> int:
        return self._objc.objc_getClass(name)

    @contextmanager
    def pool(self) -> Iterator[None]:
        pool = self._objc.objc_autoreleasePoolPush()
        try:
            yield
        finally:
            self._objc.objc_autoreleasePoolPop(pool)
