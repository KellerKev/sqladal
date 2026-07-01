"""Small string/bytes coercion helpers used by the vendored validators.

These mirror the same-named helpers in pydal so the vendored
``sqladal/validators.py`` (copied from pydal, BSD-3) imports unchanged.
"""
from __future__ import annotations

import datetime
from typing import Any, Optional


def utcnow() -> datetime.datetime:
    """Current UTC time as a naive datetime (what pydal stores)."""
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


def to_bytes(obj: Any, charset: str = "utf-8", errors: str = "strict") -> Optional[bytes]:
    if obj is None:
        return None
    if isinstance(obj, (bytes, bytearray, memoryview)):
        return bytes(obj)
    if isinstance(obj, str):
        return obj.encode(charset, errors)
    raise TypeError("Expected bytes")


def to_native(obj: Any, charset: str = "utf8", errors: str = "strict") -> Optional[str]:
    if obj is None or isinstance(obj, str):
        return obj
    return obj.decode(charset, errors)


def to_unicode(obj: Any, charset: str = "utf-8", errors: str = "strict") -> Optional[str]:
    if obj is None:
        return None
    if not hasattr(obj, "decode") or not callable(obj.decode):
        return str(obj)
    return obj.decode(charset, errors)
