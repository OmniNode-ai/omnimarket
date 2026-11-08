# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay parity (OMN-22921): recorded tick snapshots replayed through the tick orchestrator
give the same decision per pull request as the recorded decisions, and a planted divergence
turns the reading red naming the pull request."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_decision import (
    ModelLandingDecision,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    EnumTickStatus,
    ModelTickRequest,
    ModelTickSnapshot,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).resolve().parents[3] / "fixtures" / "pr_landing_tick_replay"
SNAPSHOTS = sorted(FIXTURES.glob("*.json"))
REAL_TICK = FIXTURES / "tick_725_omnimarket.json"


def _snapshot(path: Path) -> ModelTickSnapshot:
    return ModelTickSnapshot.model_validate_json(path.read_text(encoding="utf-8"))


def _request(snapshot: ModelTickSnapshot, **over: object) -> ModelTickRequest:
    base = {
        "correlation_id": uuid4(),
        "mode": EnumTickMode.REPLAY,
        "facts": snapshot.facts,
        "recorded_decision": snapshot.decision,
        "fair_share": snapshot.fair_share,
    }
    return ModelTickRequest.model_validate({**base, **over})


def test_the_corpus_holds_the_real_recorded_tick_and_scenario_ticks() -> None:
    assert REAL_TICK in SNAPSHOTS
    assert len(SNAPSHOTS) >= 6
    real = _snapshot(REAL_TICK)
    assert len(real.facts.prs) == 25
    assert len(real.decision.gates) > 0


@pytest.mark.parametrize("path", SNAPSHOTS, ids=lambda p: p.stem)
def test_replay_gives_the_recorded_decision_for_every_pull_request(path: Path) -> None:
    snapshot = _snapshot(path)
    result = HandlerPrLandingTickOrchestrator().handle(_request(snapshot))
    assert result.status is EnumTickStatus.COMPARED
    assert result.divergences == ()
    assert result.equal is True
    assert result.writes_performed == 0
    assert result.tick == snapshot.facts.tick


def test_divergence_in_one_snapshot_turns_the_parity_red_and_names_the_pull_request() -> (
    None
):
    snapshot = _snapshot(REAL_TICK)
    target = snapshot.decision.gates[0].pr
    planted = snapshot.decision.model_copy(
        update={"gates": tuple(g for g in snapshot.decision.gates if g.pr != target)}
    )
    assert isinstance(planted, ModelLandingDecision)
    result = HandlerPrLandingTickOrchestrator().handle(
        _request(snapshot, recorded_decision=planted)
    )
    assert result.status is EnumTickStatus.COMPARED
    assert result.equal is False
    assert [d.subject for d in result.divergences] == [target]
    assert all(d.path.startswith("$.gates") for d in result.divergences[0].differences)


def test_divergence_in_a_planted_action_names_the_pull_request_it_acts_on() -> None:
    path = FIXTURES / "scenario_s07_fix_submitted_awaiting_ci.json"
    snapshot = _snapshot(path)
    target = snapshot.decision.actions[0].subject
    planted = snapshot.decision.model_copy(update={"actions": ()})
    result = HandlerPrLandingTickOrchestrator().handle(
        _request(snapshot, recorded_decision=planted)
    )
    assert result.equal is False
    assert target in [d.subject for d in result.divergences]


def test_divergence_leaves_every_other_pull_request_equal() -> None:
    snapshot = _snapshot(REAL_TICK)
    first = snapshot.decision.gates[0].pr
    planted = snapshot.decision.model_copy(
        update={"gates": snapshot.decision.gates[1:]}
    )
    result = HandlerPrLandingTickOrchestrator().handle(
        _request(snapshot, recorded_decision=planted)
    )
    assert {d.subject for d in result.divergences} == {first}
    assert len(result.rows) > 1
