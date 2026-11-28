# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""beta_board: C16 and C30 read from the newest scheduled run's reading, never a cloud job run, and never from one over two hours old.

The probe tick writes a history line per run. A gateway-only check (C16, C30) has no machine half, so its line carries its whole
verdict as ``gateway_leg``; the board reads that line. A reading older than two hours is ``unread``, not a stale verdict.
"""

from __future__ import annotations

import copy
from datetime import timedelta
from pathlib import Path

import pytest

from omnimarket.models.overlay_documents.model_delegation_check_probe_overlay import (
    CheckId,
)
from omnimarket.nodes.node_beta_board_effect.handlers.adapter_beta_board_sources import (
    refresh_probes,
)
from omnimarket.nodes.node_beta_board_effect.handlers.compute_beta_board import (
    build_board,
)
from omnimarket.nodes.node_beta_board_effect.handlers.handler_beta_board import (
    OWN_CONTRACT,
    load_runtime,
)
from omnimarket.nodes.node_beta_board_effect.models.model_beta_board import (
    ModelBetaBoardOverlay,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    EnumCheckVerdict,
    ModelCheckClause,
    ModelDelegationCheckResult,
)
from tests.nodes.node_beta_board_effect.support import (
    NOW,
    OVERLAY,
    FakeCheckResultSource,
    ago,
    run_list_answer,
)

CLOUD_RED = run_list_answer([("completed", "failure", ago(minutes=5))])


def _result(
    check: CheckId, verdict: EnumCheckVerdict, minutes: int
) -> ModelDelegationCheckResult:
    ok = verdict is EnumCheckVerdict.PASS
    at = NOW - timedelta(minutes=minutes)
    return ModelDelegationCheckResult(
        correlation_id="c",
        check_id=check,
        run_id=f"run-{check}-{minutes}",
        created_at=at,
        finished_at=at,
        event="schedule",
        status="errored" if verdict is EnumCheckVerdict.ERROR else "completed",
        conclusion="success" if ok else "failure",
        verdict=verdict,
        clauses=(ModelCheckClause(name="receipt_identity", ok=ok, detail="d"),),
    )


def _runtime(check: str):
    doc = copy.deepcopy(OVERLAY)
    doc["criteria"] = [
        {
            "id": check,
            "condition": "c",
            "proof": "p",
            "owner_ticket": "OMN-1",
            "probes": [f"{check}-probe"],
        }
    ]
    doc["probes"] = [
        {
            "id": f"{check}-probe",
            "repository": "Org/cloudrepo",
            "workflow": "cloud.yml",
            "max_age_seconds": 86400,
            "gateway_check": check,
            "events": ["schedule"],
        }
    ]
    doc["ledger_readings"] = []
    return load_runtime(
        OWN_CONTRACT, environ={}, overlay=ModelBetaBoardOverlay.model_validate(doc)
    )


def _board_state(tmp_path: Path, check: str, results: list[ModelDelegationCheckResult]):
    rt = _runtime(check)
    source = FakeCheckResultSource(
        records=[r.model_dump(mode="json", exclude={"emitted"}) for r in results]
    )
    calls: list[list[str]] = []

    def run(argv, _timeout):
        calls.append(list(argv))
        return 0, CLOUD_RED + "\n", ""

    reads = refresh_probes(rt, tmp_path, NOW, run, lambda: NOW, check_results=source)
    board = build_board(rt.overlay, {r.source_id: r for r in reads.reads}, NOW)
    assert calls == [], "a scheduled-run probe never reads the cloud job run"
    assert source.calls == [rt.check_result_topic]
    return board.criteria[0]


@pytest.mark.parametrize("check", ["C16", "C30"])
def test_scheduled_pass_beats_a_stale_cloud_fail(
    tmp_path: Path, check: CheckId
) -> None:
    got = _board_state(tmp_path, check, [_result(check, EnumCheckVerdict.PASS, 10)])
    assert got.state == "pass"


@pytest.mark.parametrize("check", ["C16", "C30"])
def test_scheduled_fail_reads_fail(tmp_path: Path, check: CheckId) -> None:
    got = _board_state(tmp_path, check, [_result(check, EnumCheckVerdict.FAIL, 10)])
    assert got.state == "fail"


@pytest.mark.parametrize(
    "verdict", [EnumCheckVerdict.NOT_MEASURED, EnumCheckVerdict.PRECONDITION_ABSENT]
)
def test_an_unmeasured_run_is_never_a_pass(
    tmp_path: Path, verdict: EnumCheckVerdict
) -> None:
    got = _board_state(tmp_path, "C16", [_result("C16", verdict, 10)])
    assert got.state == "unread"


@pytest.mark.parametrize(("minutes", "expected"), [(119, "pass"), (121, "unread")])
def test_a_reading_older_than_two_hours_is_unread(
    tmp_path: Path, minutes: int, expected: str
) -> None:
    got = _board_state(
        tmp_path, "C30", [_result("C30", EnumCheckVerdict.PASS, minutes)]
    )
    assert got.state == expected
    if expected == "unread":
        assert "two hours" in got.reason


def test_one_scheduled_run_of_ten_passes_shows_ten_of_ten(tmp_path: Path) -> None:
    """The board fed one scheduled run of ten passes reads every check pass, from one bus read."""
    checks = [f"C{n}" for n in (11, 12, 13, 14, 15, 16, 17, 28, 29, 30)]
    doc = copy.deepcopy(OVERLAY)
    doc["criteria"] = [
        {
            "id": c,
            "condition": "c",
            "proof": "p",
            "owner_ticket": "OMN-1",
            "probes": [f"{c}-probe"],
        }
        for c in checks
    ]
    doc["probes"] = [
        {
            "id": f"{c}-probe",
            "repository": "Org/cloudrepo",
            "workflow": "cloud.yml",
            "max_age_seconds": 7200,
            "gateway_check": c,
            "events": ["schedule"],
        }
        for c in checks
    ]
    doc["ledger_readings"] = []
    rt = load_runtime(
        OWN_CONTRACT, environ={}, overlay=ModelBetaBoardOverlay.model_validate(doc)
    )
    source = FakeCheckResultSource(
        records=[
            _result(c, EnumCheckVerdict.PASS, 10).model_dump(
                mode="json", exclude={"emitted"}
            )
            for c in checks
        ]
    )

    def run(argv, _timeout):
        raise AssertionError("no cloud read")

    reads = refresh_probes(rt, tmp_path, NOW, run, lambda: NOW, check_results=source)
    board = build_board(rt.overlay, {r.source_id: r for r in reads.reads}, NOW)
    assert [c.state for c in board.criteria] == ["pass"] * 10
    assert source.calls == [rt.check_result_topic]
