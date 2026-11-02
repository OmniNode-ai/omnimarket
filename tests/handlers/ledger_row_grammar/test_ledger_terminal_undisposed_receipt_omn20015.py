# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
# Dropped no source tests or cases; all exercise grammar functions with temporary receipts.
"""TERMINAL rows require dispositions for the delegation receipts they name."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers import ledger_row_grammar as g

pytestmark = pytest.mark.unit
REGISTRY_ENV = "OMNI_HOME"
TS = "2026-09-29T01:00:00Z"
LANE = "lane-a"
TERMINAL = (
    f"{TS} | TERMINAL | lane={LANE} | actor=claude | model=sonnet | friction=none"
)
RUN = "11111111-1111-4111-8111-111111111111"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(REGISTRY_ENV, str(tmp_path))
    return tmp_path


def receipt(home: Path, name: str, lane: str = LANE, **fields: Any) -> Path:
    d = home / ".claude_scratch" / "delegation-receipts" / lane
    d.mkdir(parents=True, exist_ok=True)
    rec: dict[str, Any] = {
        "schema": "delegation-receipt/2",
        "lane": lane,
        "engine": "lab",
        "status": "ok",
        "run_id": RUN,
        "disposition": None,
    }
    rec.update(fields)
    p = d / f"{name}.json"
    p.write_text(json.dumps(rec), encoding="utf-8")
    return p


def test_terminal_naming_an_undisposed_receipt_is_refused(home: Path) -> None:
    p = receipt(home, "2026-09-29T010000Z-lab-evidence")
    row = f"{TERMINAL} | delegated=1 runs={RUN} lab=1 glm=0"
    reason = g.delegation_cell_reason(row)
    assert reason is not None
    assert "undisposed" in reason
    assert p.name in reason
    assert "dispose" in reason
    assert any("undisposed" in r for r in g.row_refusals(row))


def test_terminal_is_accepted_once_the_receipt_is_disposed(home: Path) -> None:
    receipt(home, "r1", disposition="accepted", reason_code="correct_as_is")
    row = f"{TERMINAL} | delegated=1 runs={RUN} lab=1 glm=0"
    assert g.delegation_cell_reason(row) is None
    assert g.row_refusals(row) == []


@pytest.mark.parametrize(
    ("cell", "fields"),
    [
        ("codex=T-1", {"engine": "codex", "thread_id": "T-1", "run_id": ""}),
        ("jev=corr-1", {"engine": "jev", "correlation_id": "corr-1", "run_id": ""}),
    ],
)
def test_codex_and_jev_ids_are_matched(
    home: Path, cell: str, fields: dict[str, Any]
) -> None:
    receipt(home, "r2", **fields)
    reason = g.delegation_cell_reason(f"{TERMINAL} | delegated=1 {cell}")
    assert reason is not None
    assert "undisposed" in reason


def test_only_named_ok_v2_real_engine_receipts_count(home: Path) -> None:
    receipt(home, "v1", schema="delegation-receipt/1")
    receipt(home, "failed", status="failed")
    receipt(home, "na", engine="na", status="na")
    receipt(home, "other-run", run_id="99999999-9999-4999-8999-999999999999")
    receipt(home, "other-lane", lane="lane-b")
    assert (
        g.delegation_cell_reason(f"{TERMINAL} | delegated=1 runs={RUN} lab=1 glm=0")
        is None
    )


def test_no_receipt_dir_or_no_registry_does_not_refuse(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row = f"{TERMINAL} | delegated=1 runs={RUN} lab=1 glm=0"
    assert g.delegation_cell_reason(row) is None
    receipt(home, "r3")
    monkeypatch.delenv(REGISTRY_ENV)
    assert g.delegation_cell_reason(row) is None


def test_na_and_script_rows_are_not_checked(home: Path) -> None:
    receipt(home, "r4")
    assert (
        g.delegation_cell_reason(f"{TERMINAL} | delegation=na:owned-elsewhere") is None
    )
    cells = f"delegated=1 runs={RUN} lab=1 glm=0"
    script = (
        f"{TS} | TERMINAL | lane={LANE} | actor=script:drain | friction=none | {cells}"
    )
    assert g.delegation_cell_reason(script) is None
