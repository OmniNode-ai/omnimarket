# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""beta_board: a criterion whose probe names a gateway check is read from that check's gateway leg, never from the cloud workflow.

Ruling 2026-11-24T13:11:36Z: a delegation check that depends on the cloud is read through the gateway. The delegation check probe node
records, on each run's history line, the verdict of the gateway leg (``gateway_leg``); the board reads that line. A green gateway leg
beside a red cloud workflow reads green; a red gateway leg reads red.
"""

from __future__ import annotations

import copy
import json
from datetime import timedelta
from pathlib import Path

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
    ModelBetaBoardRuntime,
)
from omnimarket.nodes.node_delegation_check_probe_effect.models.model_delegation_check_probe import (
    ModelCheckClause,
    gateway_leg_verdict,
)
from tests.nodes.node_beta_board_effect.support import (
    NOW,
    OVERLAY,
    ago,
    run_list_answer,
)

CLOUD_RED = run_list_answer([("completed", "failure", ago(minutes=5))])


def _runtime() -> ModelBetaBoardRuntime:
    doc = copy.deepcopy(OVERLAY)
    doc["criteria"] = [
        {
            "id": "C13",
            "condition": "customer-local delegation",
            "proof": "run record",
            "owner_ticket": "OMN-1",
            "probes": ["c13-gateway"],
        }
    ]
    doc["probes"] = [
        {
            "id": "c13-gateway",
            "repository": "Org/cloudrepo",
            "workflow": "c13.yml",
            "max_age_seconds": 7200,
            "gateway_check": "C13",
        }
    ]
    doc["ledger_readings"] = []
    return load_runtime(
        OWN_CONTRACT, environ={}, overlay=ModelBetaBoardOverlay.model_validate(doc)
    )


def _history(
    home: Path, rt: ModelBetaBoardRuntime, lines: list[dict[str, str]]
) -> None:
    path = home / rt.gateway_history_relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def _line(
    check: str, leg: str, minutes: int, event: str = "schedule"
) -> dict[str, str]:
    return {
        "check": check,
        "run_id": f"run-{check}-{minutes}",
        "created_at": (NOW - timedelta(minutes=minutes)).isoformat(),
        "event": event,
        "gateway_leg": leg,
    }


def _board_state(tmp_path: Path, lines: list[dict[str, str]]) -> tuple[str, list[str]]:
    rt = _runtime()
    if lines:
        _history(tmp_path, rt, lines)
    cloud_calls: list[list[str]] = []

    def run(argv, _timeout):
        cloud_calls.append(list(argv))
        return 0, CLOUD_RED + "\n", ""

    reads = refresh_probes(rt, tmp_path, NOW, run, lambda: NOW)
    board = build_board(rt.overlay, {r.source_id: r for r in reads.reads}, NOW)
    assert cloud_calls == [], "a gateway probe never reads the cloud workflow"
    return board.criteria[0].state, [s.detail for s in board.criteria[0].sources]


def test_green_gateway_leg_with_red_cloud_workflow_reads_green(tmp_path: Path) -> None:
    state, _ = _board_state(tmp_path, [_line("C13", "pass", 10)])
    assert state == "pass"


def test_red_gateway_leg_reads_red(tmp_path: Path) -> None:
    state, _ = _board_state(
        tmp_path, [_line("C13", "fail", 10), _line("C13", "pass", 70)]
    )
    assert state == "fail"


def test_other_checks_lines_and_unmeasured_legs_are_not_readings(
    tmp_path: Path,
) -> None:
    state, _ = _board_state(
        tmp_path,
        [
            _line("C14", "pass", 1),
            _line("C13", "unmeasured", 2),
            _line("C13", "fail", 20),
        ],
    )
    assert state == "fail"


def test_no_history_reads_unread_never_pass(tmp_path: Path) -> None:
    state, details = _board_state(tmp_path, [])
    assert state == "unread"
    assert details
    assert "history" in details[0]


def test_gateway_leg_older_than_the_bound_reads_stale(tmp_path: Path) -> None:
    state, _ = _board_state(tmp_path, [_line("C13", "pass", 60 * 5)])
    assert state == "stale"


def test_a_gateway_probe_is_not_listed_as_read_from_the_cloud(tmp_path: Path) -> None:
    rt = _runtime()
    _history(tmp_path, rt, [_line("C13", "pass", 10)])
    reads = refresh_probes(rt, tmp_path, NOW, lambda *_: (1, "", ""), lambda: NOW)
    board = build_board(rt.overlay, {r.source_id: r for r in reads.reads}, NOW)
    assert board.cloud_probes == ()


def _clause(name: str, ok: bool) -> ModelCheckClause:
    return ModelCheckClause(name=name, ok=ok, detail="d")


def test_gateway_leg_verdict_known_good_and_known_bad() -> None:
    machine = _clause("zero_provider", True)
    good = (machine, _clause("gateway_delegation_completes", True))
    bad = (machine, _clause("gateway_delegation_completes", False))
    assert gateway_leg_verdict(good) == "pass"
    assert gateway_leg_verdict(bad) == "fail"
    # A red machine clause is not the gateway's: the leg reads on its own clauses.
    assert gateway_leg_verdict((_clause("zero_provider", False), good[1])) == "pass"
    # No gateway clause graded (the leg was not measured): never a pass.
    assert gateway_leg_verdict((machine,)) == "unmeasured"
    assert gateway_leg_verdict(()) == "unmeasured"
