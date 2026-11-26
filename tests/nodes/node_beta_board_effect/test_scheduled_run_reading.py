# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""beta_board: C16 and C30 read from the newest scheduled run's reading, never a cloud job run, and never from one over two hours old.

The probe tick writes a history line per run. A gateway-only check (C16, C30) has no machine half, so its line carries its whole
verdict as ``gateway_leg``; the board reads that line. A reading older than two hours is ``unread``, not a stale verdict.
"""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path

import pytest

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
from omnimarket.nodes.node_delegation_check_probe_effect.runtime_delegation_check_probe_tick import (
    append_history,
)
from tests.nodes.node_beta_board_effect.support import (
    NOW,
    OVERLAY,
    ago,
    run_list_answer,
)

CLOUD_RED = run_list_answer([("completed", "failure", ago(minutes=5))])


def _result(
    check: str, verdict: EnumCheckVerdict, minutes: int
) -> ModelDelegationCheckResult:
    ok = verdict is EnumCheckVerdict.PASS
    at = NOW - timedelta(minutes=minutes)
    return ModelDelegationCheckResult(
        correlation_id="c",
        check_id=check,  # type: ignore[arg-type]
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
    path = tmp_path / rt.gateway_history_relpath
    append_history(path, results, 100)
    calls: list[list[str]] = []

    def run(argv, _timeout):
        calls.append(list(argv))
        return 0, CLOUD_RED + "\n", ""

    reads = refresh_probes(rt, tmp_path, NOW, run, lambda: NOW)
    board = build_board(rt.overlay, {r.source_id: r for r in reads.reads}, NOW)
    assert calls == [], "a scheduled-run probe never reads the cloud job run"
    return board.criteria[0]


@pytest.mark.parametrize("check", ["C16", "C30"])
def test_scheduled_pass_beats_a_stale_cloud_fail(tmp_path: Path, check: str) -> None:
    got = _board_state(tmp_path, check, [_result(check, EnumCheckVerdict.PASS, 10)])
    assert got.state == "pass"


@pytest.mark.parametrize("check", ["C16", "C30"])
def test_scheduled_fail_reads_fail(tmp_path: Path, check: str) -> None:
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


def test_the_history_line_of_a_gateway_only_check_names_its_leg(tmp_path: Path) -> None:
    path = tmp_path / "h.jsonl"
    append_history(
        path,
        [
            _result("C16", EnumCheckVerdict.PASS, 1),
            _result("C30", EnumCheckVerdict.FAIL, 1),
        ],
        10,
    )
    lines = [json.loads(x) for x in path.read_text().splitlines()]
    assert [x["gateway_leg"] for x in lines] == ["pass", "fail"]
