# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Context-local audit boundary for Python tool stubs, before side effects occur.

This guards the injected stub adapter, not arbitrary native programs: no proposed
command is executed. The fixed model caller and bus run outside this boundary.
"""

from __future__ import annotations

import contextlib
import contextvars
import hashlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path


class EgressBlockedError(RuntimeError):
    """A stub attempted an effect outside its one scratch ledger."""


_ACTIVE: contextvars.ContextVar[tuple[Path, list[str]] | None] = contextvars.ContextVar(
    "continuation_stub_boundary", default=None
)


def _audit(event: str, args: tuple[object, ...]) -> None:
    active = _ACTIVE.get()
    if active is None:
        return
    scratch, attempts = active
    blocked = event.startswith(
        ("socket.", "subprocess.", "os.exec", "os.spawn")
    ) or event in {
        "os.system",
        "os.fork",
        "os.forkpty",
        "os.posix_spawn",
        "os.remove",
        "os.rmdir",
        "os.rename",
        "os.link",
        "os.symlink",
        "os.truncate",
        "os.chmod",
        "os.chown",
        "os.mkdir",
        "os.chdir",
        "ctypes.dlopen",
        "ctypes.dlsym",
    }
    if event == "open":
        raw = args[0]
        if isinstance(raw, (str, bytes, os.PathLike)):
            blocked = Path(os.fsdecode(raw)).resolve() != scratch
        else:
            blocked = True
    if blocked:
        attempts.append(event)
        raise EgressBlockedError(event)


sys.addaudithook(_audit)


def file_digest(path: str) -> str:
    if not os.path.exists(path):
        return "ABSENT"
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@contextlib.contextmanager
def deny_egress(attempts: list[str], scratch_ledger: Path) -> Iterator[None]:
    token = _ACTIVE.set((scratch_ledger.resolve(), attempts))
    try:
        yield
    finally:
        _ACTIVE.reset(token)
