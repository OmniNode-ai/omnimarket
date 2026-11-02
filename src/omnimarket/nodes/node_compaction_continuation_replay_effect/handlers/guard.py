# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Egress guard around the stubs: any socket or subprocess attempt during a replay is recorded
and blocked, so a stub that lets one call through is caught, not trusted."""

from __future__ import annotations

import contextlib
import hashlib
import os
import socket
import subprocess
from collections.abc import Iterator
from typing import Any


class EgressBlockedError(RuntimeError):
    pass


def file_digest(path: str) -> str:
    """Digest of a file, or a marker for an absent one; for the protected-path audit."""
    if not os.path.exists(path):
        return "ABSENT"
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


@contextlib.contextmanager
def deny_egress(attempts: list[str]) -> Iterator[None]:
    def _blocked(kind: str) -> Any:
        def _raise(*args: object, **kwargs: object) -> Any:
            attempts.append(kind)
            raise EgressBlockedError(kind)

        return _raise

    saved: list[tuple[object, str, object]] = []

    def _patch(owner: object, name: str, kind: str) -> None:
        saved.append((owner, name, getattr(owner, name)))
        setattr(owner, name, _blocked(kind))

    _patch(socket.socket, "connect", "socket.connect")
    _patch(socket.socket, "connect_ex", "socket.connect_ex")
    _patch(socket, "create_connection", "socket.create_connection")
    _patch(subprocess.Popen, "__init__", "subprocess.Popen")
    try:
        yield
    finally:
        for owner, name, original in reversed(saved):
            setattr(owner, name, original)
