# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger snapshot a lane start stages on its host (OMN-23528).

A pure port of the remote-lane runner's ``build_ledger_snapshot``: a gzip tar of the live ledger file
and its sibling ``archive/*.md`` rolls. The live ledger is whole rows only: a row being appended is cut
at the last newline and never shipped; the rolls go in sorted by name as ``archive/<name>``. Every member
is mode 0o444 with the mtime of the injected clock, and the base64 of the tar is what the ssh
transport's ``stage_ledger_snapshot`` carries in the request's ``content`` and unpacks on the host.
"""

from __future__ import annotations

import base64
import io
import re
import tarfile
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_FROZEN = ConfigDict(frozen=True, extra="forbid")

#: A plain ledger file name: starts with a letter or digit, plain characters, and ends ``.md``; a path
#: separator or any other shape is refused.
_LEDGER_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.md")


class LedgerSnapshotError(ValueError):
    """The snapshot cannot be made: the name is not a plain ledger name, or a read of the ledger or a roll failed."""


class ModelLedgerSnapshot(BaseModel):
    """The staged snapshot: the base64 gzip tar and what it holds."""

    model_config = _FROZEN

    content: str
    ledger: str
    ledger_bytes: int
    archive_files: int
    tar_bytes: int


def build_ledger_snapshot(
    ledger: Path, *, now: Callable[[], float] = time.time
) -> ModelLedgerSnapshot:
    """The whole rows of ``ledger`` and the ``archive`` rolls beside it, as one read-only gzip tar.

    The live content is the file's bytes cut at the last newline, so a row being appended is never
    shipped. Raises ``LedgerSnapshotError`` naming the path when the file name is not a plain ledger
    name or a read fails.
    """
    name = ledger.name
    if not _LEDGER_NAME_RE.fullmatch(name):
        raise LedgerSnapshotError(
            f"ledger {ledger}: name {name!r} is not a plain ledger file name matching {_LEDGER_NAME_RE.pattern}"
        )
    try:
        live = ledger.read_bytes()
    except OSError as exc:
        raise LedgerSnapshotError(f"cannot read ledger {ledger}: {exc}") from exc
    live = live[: live.rfind(b"\n") + 1]
    members: list[tuple[str, bytes]] = [(name, live)]
    archive_dir = ledger.parent / "archive"
    rolls = (
        [p for p in archive_dir.glob("*.md") if p.is_file()]
        if archive_dir.is_dir()
        else []
    )
    for roll in sorted(rolls, key=lambda p: p.name):
        try:
            payload = roll.read_bytes()
        except OSError as exc:
            raise LedgerSnapshotError(
                f"cannot read archive roll {roll}: {exc}"
            ) from exc
        members.append((f"archive/{roll.name}", payload))
    mtime = int(now())
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz", compresslevel=3) as tar:
        for member_name, payload in members:
            info = tarfile.TarInfo(name=member_name)
            info.size = len(payload)
            info.mode = 0o444
            info.mtime = mtime
            tar.addfile(info, io.BytesIO(payload))
    tar_bytes = buf.getvalue()
    return ModelLedgerSnapshot(
        content=base64.b64encode(tar_bytes).decode("ascii"),
        ledger=name,
        ledger_bytes=len(live),
        archive_files=len(rolls),
        tar_bytes=len(tar_bytes),
    )


__all__ = [
    "LedgerSnapshotError",
    "ModelLedgerSnapshot",
    "build_ledger_snapshot",
]
