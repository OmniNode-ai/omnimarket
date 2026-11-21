# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""detail_capture_bound: an entrypoint detail capture keeps head, tail and a dropped-line count.

A capture that passes the size bound is cut to its first head_lines and last tail_lines and ends
with one line that states how many lines were dropped. The bound and both sizes are values of the
node contract's ``detail_capture`` section, never literals in code. The 200 MB stream is generated
lazily inside the test and never committed.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_work_ledger_reader_effect.handlers.detail_capture_bound import (
    bound_capture,
    load_detail_capture_settings,
)

_CONTRACT = (
    Path(__file__).resolve().parents[2]
    / "src/omnimarket/nodes/node_work_ledger_reader_effect/contract.yaml"
)
_LINE = b"x" * 79 + b"\n"  # 80 bytes
STREAM_BYTES = 200 * 1024 * 1024


class _Stream(io.RawIOBase):
    """A lazily generated stream of numbered 80-byte lines; nothing is held in memory."""

    def __init__(self, lines: int) -> None:
        self._lines = lines
        self._next = 0

    def readable(self) -> bool:
        return True

    def readline(self, size: int | None = -1) -> bytes:
        if self._next >= self._lines:
            return b""
        n = self._next
        self._next += 1
        return f"{n:08d}".encode() + _LINE[8:]

    def __iter__(self) -> _Stream:
        return self

    def __next__(self) -> bytes:
        line = self.readline()
        if not line:
            raise StopIteration
        return line


def test_detail_capture_bound_settings_come_from_the_contract() -> None:
    settings = load_detail_capture_settings(_CONTRACT)
    raw = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))["detail_capture"]
    assert settings.max_bytes == raw["max_bytes"]
    assert settings.head_lines == raw["head_lines"]
    assert settings.tail_lines == raw["tail_lines"]


def test_detail_capture_bound_200mb_stream_ends_under_the_bound(
    tmp_path: Path,
) -> None:
    settings = load_detail_capture_settings(_CONTRACT)
    lines = STREAM_BYTES // len(_LINE)
    dest = tmp_path / "query.detail"
    result = bound_capture(_Stream(lines), dest, settings)
    assert result.truncated is True
    assert result.total_lines == lines
    assert dest.stat().st_size < settings.max_bytes
    out = dest.read_text(encoding="utf-8").splitlines()
    dropped = lines - settings.head_lines - settings.tail_lines
    assert result.dropped_lines == dropped
    assert str(dropped) in out[-1]
    assert "dropped" in out[-1]
    assert out[0].startswith("00000000")  # the head is kept
    assert out[settings.head_lines - 1].startswith(f"{settings.head_lines - 1:08d}")
    assert any(line.startswith(f"{lines - 1:08d}") for line in out)  # the tail is kept


def test_detail_capture_bound_a_small_capture_is_written_whole(
    tmp_path: Path,
) -> None:
    settings = load_detail_capture_settings(_CONTRACT)
    dest = tmp_path / "small.detail"
    result = bound_capture(_Stream(50), dest, settings)
    assert result.truncated is False
    assert result.dropped_lines == 0
    assert len(dest.read_text(encoding="utf-8").splitlines()) == 50


def test_detail_capture_bound_edges_no_overlap_of_head_and_tail(
    tmp_path: Path,
) -> None:
    settings = load_detail_capture_settings(_CONTRACT).model_copy(
        update={"max_bytes": 1000, "head_lines": 3, "tail_lines": 3}
    )
    dest = tmp_path / "edge.detail"
    result = bound_capture(_Stream(20), dest, settings)  # 1600 bytes, over 1000
    out = dest.read_text(encoding="utf-8").splitlines()
    assert result.dropped_lines == 14
    assert [line[:8] for line in out[:3]] == ["00000000", "00000001", "00000002"]
    assert [line[:8] for line in out[3:6]] == ["00000017", "00000018", "00000019"]
    assert "14" in out[-1]


@pytest.mark.parametrize("lines", [12, 13])
def test_detail_capture_bound_fewer_lines_than_head_plus_tail_drops_none(
    tmp_path: Path, lines: int
) -> None:
    settings = load_detail_capture_settings(_CONTRACT).model_copy(
        update={"max_bytes": 100, "head_lines": 8, "tail_lines": 8}
    )
    dest = tmp_path / "few.detail"
    result = bound_capture(_Stream(lines), dest, settings)
    assert result.dropped_lines == 0
    assert len(dest.read_text(encoding="utf-8").splitlines()) >= lines
