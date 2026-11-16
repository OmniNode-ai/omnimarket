# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ledger snapshot a lane start stages on its host: whole rows and the archive rolls, as a read-only tar.

The live ledger ships whole rows only (a row being appended is cut at the last newline), the rolls beside it
go in as ``archive/<name>`` sorted by name, every member is 0o444 with the mtime of the injected clock, and
the base64 of the gzip tar is what ``stage_ledger_snapshot`` carries in the request's ``content``.
"""

from __future__ import annotations

import base64
import io
import re
import tarfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from omnimarket.nodes.node_remote_lane_start_orchestrator.orchestration.ledger_snapshot import (
    LedgerSnapshotError,
    ModelLedgerSnapshot,
    build_ledger_snapshot,
)

pytestmark = [pytest.mark.unit]

NOW = 1_700_000_000.0


def _members(b64: str) -> dict[str, bytes]:
    raw = base64.b64decode(b64, validate=True)
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        for member in tar.getmembers():
            handle = tar.extractfile(member) if member.isfile() else None
            out[member.name] = handle.read() if handle is not None else b""
    return out


def _meta(b64: str) -> list[tuple[str, int, int]]:
    """(name, mode, mtime) of every member, in the order the tar holds them."""
    raw = base64.b64decode(b64, validate=True)
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as tar:
        return [(m.name, m.mode, int(m.mtime)) for m in tar.getmembers()]


def test_whole_rows_only_and_read_only_members(tmp_path: Path) -> None:
    ledger = tmp_path / "floor.md"
    whole = (
        "2026-10-01T00:00:00Z | CLAIM | lane=floor | run=alpha\n"
        "2026-10-01T00:05:00Z | TERMINAL | lane=floor | run=alpha\n"
    )
    ledger.write_bytes(
        whole.encode() + b"2026-10-01T00:05:01Z | STATUS | lane=floor | appended"
    )
    snap = build_ledger_snapshot(ledger, now=lambda: NOW)

    assert isinstance(snap, ModelLedgerSnapshot)
    assert snap.ledger == "floor.md"
    assert snap.ledger_bytes == len(whole.encode())
    assert snap.tar_bytes == len(base64.b64decode(snap.content, validate=True))
    # zero: no archive directory beside the ledger
    assert snap.archive_files == 0
    [member] = _meta(snap.content)
    assert member == ("floor.md", 0o444, int(NOW))
    assert _members(snap.content)["floor.md"] == whole.encode()
    with pytest.raises(ValidationError):
        snap.ledger = "other.md"

    # positive control of the zero: a roll beside the ledger is counted and ships
    (tmp_path / "archive").mkdir()
    (tmp_path / "archive" / "2026-09-30.md").write_bytes(b"2026-09-30 | rolled\n")
    with_rolls = build_ledger_snapshot(ledger, now=lambda: NOW)
    assert with_rolls.archive_files == 1
    assert with_rolls.tar_bytes > snap.tar_bytes


def test_archive_rolls_are_sorted_and_named(tmp_path: Path) -> None:
    ledger = tmp_path / "floor.md"
    ledger.write_bytes(b"2026-10-01 | CLAIM | lane=floor\n")
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "2026-10-02.md").write_bytes(b"2026-10-02 | rolled\n")
    (archive / "2026-10-01.md").write_bytes(b"2026-10-01 | rolled\n")
    (archive / "not-a-roll.txt").write_bytes(b"notes\n")
    snap = build_ledger_snapshot(ledger, now=lambda: NOW)

    assert snap.archive_files == 2
    assert [name for name, _, _ in _meta(snap.content)] == [
        "floor.md",
        "archive/2026-10-01.md",
        "archive/2026-10-02.md",
    ]
    assert _members(snap.content) == {
        "floor.md": b"2026-10-01 | CLAIM | lane=floor\n",
        "archive/2026-10-01.md": b"2026-10-01 | rolled\n",
        "archive/2026-10-02.md": b"2026-10-02 | rolled\n",
    }
    assert all(mode == 0o444 for _, mode, _ in _meta(snap.content))

    # positive control of the exclusion: a non-.md roll is not counted once no .md roll stands
    (archive / "2026-10-01.md").unlink()
    (archive / "2026-10-02.md").unlink()
    bare = build_ledger_snapshot(ledger, now=lambda: NOW)
    assert bare.archive_files == 0
    assert "not-a-roll.txt" not in _members(bare.content)


@pytest.mark.parametrize(
    "name",
    [
        "sub/floor.md",  # a path separator
        "floor",  # without .md
        "floor.txt",
        ".md",
        " floor.md",
        "floor.md.bak",
    ],
)
def test_ledger_name_must_be_plain(tmp_path: Path, name: str) -> None:
    with pytest.raises(LedgerSnapshotError):
        build_ledger_snapshot(tmp_path / name, now=lambda: NOW)


def test_missing_ledger_names_the_path(tmp_path: Path) -> None:
    ledger = tmp_path / "missing.md"
    with pytest.raises(LedgerSnapshotError, match=re.escape(str(ledger))):
        build_ledger_snapshot(ledger, now=lambda: NOW)


def test_a_row_cut_mid_line_is_never_shipped(tmp_path: Path) -> None:
    """The half-written row must never reach the tar member.

    Failing control: with the cut at the last newline (``live[: live.rfind(b"\\n") + 1]``) removed from
    ``build_ledger_snapshot``, the half-written row below would be in the member and this test would fail.
    """
    half = "2026-10-01T00:05:01Z | STATUS | lane=floor | engine=still-appending"
    ledger = tmp_path / "floor.md"
    ledger.write_bytes(b"2026-10-01T00:00:00Z | CLAIM | lane=floor\n" + half.encode())
    snap = build_ledger_snapshot(ledger, now=lambda: NOW)

    data = _members(snap.content)["floor.md"]
    assert half.encode() not in data
    assert data == b"2026-10-01T00:00:00Z | CLAIM | lane=floor\n"
