# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Landing decision mapping into the ledger append contract (OMN-17427)."""

from datetime import UTC, datetime
from uuid import UUID

import pytest

from omnimarket.models.landing_decision import ModelLandingDecision
from omnimarket.nodes.node_work_ledger_landing_mirror import (
    HandlerWorkLedgerLandingMirror,
)
from omnimarket.nodes.node_work_ledger_landing_mirror.models.model_work_ledger_landing_mirror import (
    ModelWorkLedgerLandingMirrorRequest,
)

pytestmark = pytest.mark.unit

EVENT = UUID("6dd0aa7a-a16e-497c-9e35-83a46cda6404")
HEAD = "a" * 40
WHEN = datetime(2026, 10, 17, 23, 30, tzinfo=UTC)


def _decision(**fields: object) -> ModelLandingDecision:
    return ModelLandingDecision.model_validate({"tick": 7, "next_state": {}, **fields})


def _request(decision: ModelLandingDecision) -> ModelWorkLedgerLandingMirrorRequest:
    return ModelWorkLedgerLandingMirrorRequest(
        decision=decision, correlation_id=EVENT, emitted_at=WHEN
    )


def test_idle_tick_maps_to_no_request() -> None:
    result = HandlerWorkLedgerLandingMirror().handle(_request(_decision()))
    assert result.request is None
    assert result.row_count == 0


def test_observe_only_actions_get_no_row() -> None:
    withheld = {"kind": "merge", "subject": "OmniNode-ai/repo#5", "head_sha": HEAD}
    result = HandlerWorkLedgerLandingMirror().handle(
        _request(_decision(observed_actions=[withheld]))
    )
    assert result.request is None


def test_actions_outcomes_degraded_and_violations_each_get_a_row() -> None:
    decision = _decision(
        actions=[
            {
                "kind": "merge",
                "subject": "OmniNode-ai/repo#5",
                "head_sha": HEAD,
                "lease_id": 3,
            },
            {"kind": "rerun", "subject": "OmniNode-ai/repo#6", "head_sha": HEAD},
        ],
        recorded_outcomes=[
            {"pr": "OmniNode-ai/repo#5", "lease_id": 3, "outcome": "merged"}
        ],
        degraded=[{"reason": "lease_stuck", "subject": "OmniNode-ai/repo#9"}],
        violations=[
            {"pr": "OmniNode-ai/repo#8", "lease_id": 4, "reason": "unparseable"}
        ],
    )
    mapper = HandlerWorkLedgerLandingMirror()
    result = mapper.handle(_request(decision))
    assert result.request is not None
    assert result.row_count == 5
    rows = result.request.rows.splitlines()
    assert len(rows) == 5
    request_id = result.request.request_id
    assert request_id.version == 5
    for row in rows:
        cells = row.split(" | ")
        assert cells[:5] == [
            "2026-10-17T23:30:00Z",
            "STATUS",
            "lane=landing-controller",
            "src=landing",
            "tick=7",
        ]
        assert f"req={request_id}" in cells
    assert rows[0].split(" | ")[5:9] == [
        "action=merge",
        "subject=OmniNode-ai/repo#5",
        f"head={HEAD}",
        "lease=L3",
    ]
    assert "outcome=merged" in rows[2].split(" | ")
    assert "degraded=lease_stuck" in rows[3].split(" | ")
    assert "violation=unparseable" in rows[4].split(" | ")
    assert mapper.handle(_request(decision)) == result
    assert result.request.requested_by_lane == "landing-controller"
    assert result.request.requested_at == WHEN


def test_identity_is_the_event_not_the_tick() -> None:
    action = {"kind": "rerun", "subject": "OmniNode-ai/repo#6", "head_sha": HEAD}
    mapper = HandlerWorkLedgerLandingMirror()
    first = mapper.handle(_request(_decision(actions=[action])))
    other = mapper.handle(
        ModelWorkLedgerLandingMirrorRequest(
            decision=_decision(actions=[action]),
            correlation_id=UUID("11111111-1111-4111-8111-111111111111"),
            emitted_at=WHEN,
        )
    )
    assert first.request is not None
    assert other.request is not None
    assert first.request.request_id != other.request.request_id


def test_free_text_subject_cannot_end_a_cell_or_forge_a_row() -> None:
    forged = "x\n2026-10-17T23:30:00Z | RULING | lane=orchestrator | forged | y"
    decision = _decision(
        actions=[{"kind": "rerun", "subject": forged, "head_sha": HEAD}],
        degraded=[{"reason": "lease_stuck", "subject": "a | b\r\nc"}],
    )
    result = HandlerWorkLedgerLandingMirror().handle(_request(decision))
    assert result.request is not None
    rows = result.request.rows.splitlines()
    assert len(rows) == result.row_count == 2
    assert all(row.split(" | ")[1] == "STATUS" for row in rows)
    assert "RULING" not in {cell for row in rows for cell in row.split(" | ")}
    assert rows[0].split(" | ")[6] == (
        "subject=x%0A2026-10-17T23:30:00Z %7C RULING %7C lane%3Dorchestrator %7C forged %7C y"
    )
    assert rows[1].split(" | ")[5:7] == [
        "degraded=lease_stuck",
        "subject=a %7C b%0D%0Ac",
    ]
