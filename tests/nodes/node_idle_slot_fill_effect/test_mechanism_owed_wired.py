# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The throughput tick's fill prints one owed line per abandoned mechanism ticket.

The tick reads the ledger on every run (the ruling recovery) and now asks
node_ruling_mechanism_owed_compute which mechanism tickets a lane left unfinished with no
CLAIM after one tick. The proof is the tick's own findings over the recorded ledger slice of
2026-10-24, not the compute node's unit test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_idle_slot_fill_effect.handlers.handler_idle_slot_fill import (
    HandlerLabFill,
)
from omnimarket.nodes.node_idle_slot_fill_effect.models import ModelLabFillRequest

pytestmark = pytest.mark.unit

SLICE = (
    Path(__file__).parents[1]
    / "node_ruling_mechanism_owed_compute"
    / "fixtures"
    / "ledger_slice_2026_10_24.txt"
)
RECORDED_TICK = "2026-10-24T13:30:00Z"
FOLD_KEY = "mechanism-owed:OMN-21288:2026-10-24T12:52:12Z"
LAND_KEY = "mechanism-owed:OMN-18594:2026-10-24T12:06:44Z"


def tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, text: str, now: str
) -> list[tuple[str, str, str, str]]:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text(text, encoding="utf-8")
    monkeypatch.setenv("ONEX_LEDGER_PATH", str(ledger))
    monkeypatch.setenv("OMNI_LAB_FILL_GENERATE", "off")
    backlog = tmp_path / "backlog.json"
    backlog.write_text('{"items": []}', encoding="utf-8")
    result = HandlerLabFill().handle(
        ModelLabFillRequest(
            now=now,
            request={"lab_headroom": {"hosts": []}},
            lines=[],
            launch=None,
            state_dir=tmp_path / "state",
            backlog=str(backlog),
            runner=tmp_path / "runner.py",
            parent="orchestrator-test",
            out_of_scope={},
            read_only=True,
        )
    )
    return [f for f in result.found if f[1].startswith("mechanism-owed:")]


def slice_text() -> str:
    return SLICE.read_text(encoding="utf-8")


def test_tick_prints_an_owed_line_per_unfinished_mechanism_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    owed = tick(tmp_path, monkeypatch, slice_text(), RECORDED_TICK)
    assert sorted(f[1] for f in owed) == sorted([FOLD_KEY, LAND_KEY])
    fold = next(f for f in owed if f[1] == FOLD_KEY)
    assert fold[0] == "missing"
    assert "lane=one-runtime-fold-1245" in fold[2]
    assert "ended blocked at 2026-10-24T12:52:12Z" in fold[2]
    assert fold[3], "a line the tick prints names the action that clears it"


def test_tick_prints_no_owed_line_for_a_handed_off_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = slice_text()
    handed_off = [
        line
        for line in text.splitlines()
        if " | TERMINAL | " in line
        and "ticket=OMN-19998" in line
        and "outcome=handed-off" in line
    ]
    assert handed_off, (
        "positive control: the slice holds handed-off mechanism TERMINALs"
    )
    owed = tick(tmp_path, monkeypatch, text, RECORDED_TICK)
    assert not [f for f in owed if "OMN-19998" in f[1]]
    blocked = text.replace(
        "lane=one-runtime-fold-1245 | ticket=OMN-21288 | outcome=blocked",
        "lane=one-runtime-fold-1245 | ticket=OMN-21288 | outcome=handed-off",
    )
    assert blocked != text
    assert [f[1] for f in tick(tmp_path, monkeypatch, blocked, RECORDED_TICK)] == [
        LAND_KEY
    ]


def test_tick_prints_a_failed_terminal_the_relaunch_scan_does_not_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = slice_text().replace(
        "lane=one-runtime-fold-1245 | ticket=OMN-21288 | outcome=blocked",
        "lane=one-runtime-fold-1245 | ticket=OMN-21288 | outcome=failed",
    )
    owed = tick(tmp_path, monkeypatch, text, RECORDED_TICK)
    assert FOLD_KEY in [f[1] for f in owed]


def test_a_terminal_younger_than_one_tick_is_not_yet_printed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    keys = [
        f[1] for f in tick(tmp_path, monkeypatch, slice_text(), "2026-10-24T13:20:00Z")
    ]
    assert keys == [LAND_KEY]
