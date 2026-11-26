# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The plan owner check CLI end to end on the in-memory path (OMN-24445).

OWNED (exit 0) from the fixtures' real lane-x claim through the real query node and reader
handlers; an empty state is MISSING (exit 1); a database fault is UNKNOWN (exit 2), never
MISSING. The journal receives one JSON line per finding and stderr carries the PLAN-OWNER line.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.nodes.node_plan_owner_check_effect.runtime_plan_owner_check import main
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    WorkLedgerReadError,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    NOW,
    FakeStateReader,
    claim_entity,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_open_claims_window import (
    StampedQueryNodePointRead,
)

pytestmark = pytest.mark.unit

READER_OVERLAY = """\
moved_readers: [cutover-owner]
# The CLI's handlers read the real clock and the fixtures' watermark is fixed at 2026-11-02,
# so the freshness bound is pinned wide instead of the clock.
max_watermark_age_seconds: 99999999
page_size: 200
max_pages: 50
route: {kind: inmemory}
parity: {receipt_lane: work-ledger-parity, window_hours: 48}
events: {journal_env: ONEX_LEDGER_READER_JOURNAL}
"""

OWNER_OVERLAY = """\
reader: cutover-owner
# The window must cover every rule's live_window_minutes.
claims_window_hours: 24
journal_env: ONEX_PLAN_OWNER_CHECK_JOURNAL
rules:
  - finding: cutover-plan-owner
    # main's clock is pinned to the fixtures' NOW (2026-11-02T03:00Z); the fixture claim's newest
    # row is 30 minutes older, inside this window.
    tickets: [OMN-21948]
    live_window_minutes: 600
    why: no live lane CLAIM names the cutover plan
    fix: start a ledger cutover lane
    unknown_fix: read the open-claims answer and fix the read it names
"""


def env(tmp_path: Path) -> dict[str, str]:
    (tmp_path / "reader.yaml").write_text(READER_OVERLAY)
    (tmp_path / "owner.yaml").write_text(OWNER_OVERLAY)
    return {
        "ONEX_LEDGER_READER_OVERLAY": str(tmp_path / "reader.yaml"),
        "ONEX_PLAN_OWNER_CHECK_OVERLAY": str(tmp_path / "owner.yaml"),
        "ONEX_PLAN_OWNER_CHECK_JOURNAL": str(tmp_path / "journal.jsonl"),
    }


def run(
    tmp_path: Path, state: FakeStateReader, capsys: pytest.CaptureFixture[str]
) -> tuple[int, str, str]:
    code = main(
        [],
        environ=env(tmp_path),
        point_read=StampedQueryNodePointRead(state),
        clock=lambda: NOW,
    )
    out = capsys.readouterr()
    return code, out.out, out.err


def test_the_real_lane_claim_exits_0_owned(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run(tmp_path, FakeStateReader((claim_entity(),)), capsys)
    assert (code, out.strip()) == (0, "OWNED cutover-plan-owner | lane-x")
    assert "PLAN-OWNER" in err
    assert "verdict=OWNED" in err
    [line] = (tmp_path / "journal.jsonl").read_text().splitlines()
    assert json.loads(line)["verdict"] == "OWNED"


def test_an_empty_state_exits_1_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run(tmp_path, FakeStateReader(()), capsys)
    assert (code, out.strip()) == (
        1,
        "MISSING cutover-plan-owner | no live lane CLAIM names the cutover plan"
        " | FIX: start a ledger cutover lane",
    )


def test_a_database_fault_exits_2_unknown(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = FakeStateReader(error=WorkLedgerReadError("database unavailable"))
    code, out, err = run(tmp_path, state, capsys)
    assert code == 2
    assert out.startswith("UNKNOWN cutover-plan-owner | node-read-failed:")
    assert "PLAN-OWNER" in err


def test_the_overlay_claims_window_reaches_the_point_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    read = StampedQueryNodePointRead(FakeStateReader((claim_entity(),)))
    code = main([], environ=env(tmp_path), point_read=read, clock=lambda: NOW)
    assert code == 0
    [query] = read.requests
    assert query.query == "open-claims"
    assert query.claims_since == NOW - timedelta(hours=24)


def test_a_missing_reader_overlay_exits_2_unknown_naming_the_roots(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No overlay under the roots is no read: one UNKNOWN line, never a traceback (OMN-24646)."""
    root = tmp_path / "onex-overlays-only"
    root.mkdir()
    code = main(
        [],
        environ={"ONEX_SKILL_OVERLAY_ROOTS": str(root)},
        point_read=StampedQueryNodePointRead(FakeStateReader(())),
        clock=lambda: NOW,
    )
    out = capsys.readouterr()
    assert code == 2
    [line] = out.out.splitlines()
    assert line.startswith("UNKNOWN plan-owner | node_work_ledger_reader_effect:")
    assert str(root) in line
    assert "FIX: name the overlays clone's overlays directory" in line
    assert "verdict=UNKNOWN reason=overlay-unreadable" in out.err


def test_a_missing_owner_overlay_is_reported_under_the_named_finding(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "reader.yaml").write_text(READER_OVERLAY)
    code = main(
        ["--finding", "cutover-plan-owner"],
        environ={
            "ONEX_LEDGER_READER_OVERLAY": str(tmp_path / "reader.yaml"),
            "ONEX_SKILL_OVERLAY_ROOTS": str(tmp_path),
        },
        point_read=StampedQueryNodePointRead(FakeStateReader(())),
        clock=lambda: NOW,
    )
    out = capsys.readouterr()
    assert code == 2
    assert out.out.startswith(
        "UNKNOWN cutover-plan-owner | node_plan_owner_check_effect:"
    )
