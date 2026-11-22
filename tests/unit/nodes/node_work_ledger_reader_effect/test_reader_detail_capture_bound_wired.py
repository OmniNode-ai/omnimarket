# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The reader entrypoint prints its verb lines through the detail capture bound (OMN-26395).

``handlers/detail_capture_bound.py`` holds ``bound_capture``; the entrypoint's printed lines go
through it, so a read past the contract's ``max_bytes`` ends as its head, its tail and one line that
states the dropped line count. These tests fail when the entrypoint stops calling the bound.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pytest

from omnimarket.nodes.node_work_ledger_reader_effect import (
    runtime_work_ledger_reader as entrypoint,
)
from omnimarket.nodes.node_work_ledger_reader_effect.handlers.detail_capture_bound import (
    bound_capture,
    load_detail_capture_settings,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import ModelPointAnswer
from omnimarket.nodes.node_work_ledger_reader_effect.models.model_detail_capture import (
    ModelDetailCaptureResult,
    ModelDetailCaptureSettings,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import WATERMARK
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_rows_filters_ledger_cut5 import (
    EXAMPLE,
    PagedPointRead,
)

pytestmark = pytest.mark.unit

ARGV = ["--reader", "rows", "--query", "rows", "--window-hours", "24"]


def _run(
    tmp_path: Path,
    lines: tuple[str, ...],
    settings: ModelDetailCaptureSettings | None,
) -> int:
    moved = tmp_path / "overlay.yaml"
    moved.write_text(
        EXAMPLE.read_text(encoding="utf-8").replace(
            "moved_readers: [inbox]", "moved_readers: [rows]"
        ),
        encoding="utf-8",
    )
    read = PagedPointRead(
        [ModelPointAnswer(lines=lines, matched=len(lines), watermark=WATERMARK)]
    )
    return entrypoint.main(
        ARGV,
        environ={"ONEX_LEDGER_READER_OVERLAY": str(moved)},
        point_read=read,
        detail_settings=settings,
    )


def test_detail_capture_bound_is_called_by_the_entrypoint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    calls: list[Path] = []
    real = bound_capture

    def spy(
        stream: Iterable[bytes], dest: Path, settings: ModelDetailCaptureSettings
    ) -> ModelDetailCaptureResult:
        calls.append(dest)
        return real(stream, dest, settings)

    monkeypatch.setattr(f"{entrypoint.__name__}.bound_capture", spy)
    code = _run(tmp_path, ("row-1", "row-2"), None)
    assert code in (0, 1), capsys.readouterr().err
    assert len(calls) == 1, "the entrypoint did not call bound_capture"
    assert capsys.readouterr().out == "row-1\nrow-2\n"


def test_detail_capture_bound_a_long_read_prints_head_tail_and_dropped_count(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = load_detail_capture_settings().model_copy(
        update={"max_bytes": 100, "head_lines": 2, "tail_lines": 2}
    )
    lines = tuple(f"row-{n:04d}-{'x' * 20}" for n in range(20))
    code = _run(tmp_path, lines, settings)
    captured = capsys.readouterr()
    assert code in (0, 1), captured.err
    out = captured.out.splitlines()
    assert out[:2] == list(lines[:2])
    assert out[2:4] == list(lines[-2:])
    assert "16 lines dropped" in out[-1]
    assert len(out) == 5


def test_detail_capture_bound_the_default_settings_are_the_contracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    seen: list[ModelDetailCaptureSettings] = []
    real = bound_capture

    def spy(
        stream: Iterable[bytes], dest: Path, settings: ModelDetailCaptureSettings
    ) -> ModelDetailCaptureResult:
        seen.append(settings)
        return real(stream, dest, settings)

    monkeypatch.setattr(f"{entrypoint.__name__}.bound_capture", spy)
    _run(tmp_path, ("row-1",), None)
    capsys.readouterr()
    assert seen == [load_detail_capture_settings()]
