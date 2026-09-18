# Copyright (C) 2026 mbrennwa
# SPDX-License-Identifier: GPL-3.0-or-later

"""Length-prefixed JSON IPC for Camel helper processes (#437 / #422)."""

from __future__ import annotations

import base64
import json
import struct
from typing import Any, BinaryIO

_HEADER = struct.Struct("!I")
_BYTES_KEY = "__post_bytes_b64__"


def _encode_obj(obj: Any) -> Any:
    if isinstance(obj, bytes):
        return {_BYTES_KEY: base64.b64encode(obj).decode("ascii")}
    if isinstance(obj, dict):
        return {str(k): _encode_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_encode_obj(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _decode_obj(obj: Any) -> Any:
    if isinstance(obj, dict):
        if set(obj.keys()) == {_BYTES_KEY} and isinstance(obj[_BYTES_KEY], str):
            return base64.b64decode(obj[_BYTES_KEY].encode("ascii"))
        return {k: _decode_obj(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_decode_obj(v) for v in obj]
    return obj


def write_message(stream: BinaryIO, payload: dict[str, Any]) -> None:
    raw = json.dumps(_encode_obj(payload), separators=(",", ":")).encode("utf-8")
    stream.write(_HEADER.pack(len(raw)))
    stream.write(raw)
    stream.flush()


def _read_exact(stream: BinaryIO, size: int) -> bytes | None:
    """Read ``size`` bytes, looping until complete or EOF.

    ``Popen(..., bufsize=0)`` yields raw pipes whose ``read(n)`` may return
    short of ``n`` after a single syscall (pipe capacity is often ~64 KiB).
    Treating a short read as EOF desyncs the length-prefixed stream and makes
    the UI report ``camel helper process exited`` while the helper is still
    alive mid-``write`` (#445).
    """
    if size <= 0:
        return b""
    chunks: list[bytes] = []
    remaining = size
    while remaining > 0:
        chunk = stream.read(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_message(stream: BinaryIO) -> dict[str, Any] | None:
    header = _read_exact(stream, _HEADER.size)
    if header is None:
        return None
    (length,) = _HEADER.unpack(header)
    if length <= 0 or length > 64 * 1024 * 1024:
        raise ValueError(f"invalid IPC frame length {length}")
    raw = _read_exact(stream, length)
    if raw is None:
        return None
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise TypeError("IPC payload must be a JSON object")
    decoded = _decode_obj(payload)
    if not isinstance(decoded, dict):
        raise TypeError("IPC payload must decode to an object")
    return decoded
