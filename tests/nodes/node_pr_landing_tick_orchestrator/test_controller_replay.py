# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Replay of the recorded controller ticks through the carry and the compare step (OMN-23665).

Every recorded tick of tests/fixtures/pr_landing_tick_replay (the scenario ticks and the trimmed
tick 725 of omnimarket, the controller's facts as it decided them) is cut down to what the PR watcher's
state gives (no controller state, every carried pull request field at its default), carried back from
the controller's tick and compared with the controller's decision for the repository of its first pull
request. The failing control runs the same ticks without the carry: the replay must be able to fail.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from omnimarket.models.landing_controller_carry import ModelLandingControllerCarry
from omnimarket.models.landing_decision import (
    EnumLandingCi,
    EnumLandingRedClass,
    ModelLandingFacts,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_state import (
    ModelLandingControllerState,
)
from omnimarket.nodes.node_pr_landing_snapshot_effect.handlers.handler_pr_landing_snapshot import (
    CARRIED_FACT_FIELDS,
    CARRIED_PR_FIELDS,
    carry_controller_state,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.handlers.handler_pr_landing_tick_orchestrator import (
    HandlerPrLandingTickOrchestrator,
)
from omnimarket.nodes.node_pr_landing_tick_orchestrator.models.model_tick_orchestrator import (
    EnumTickMode,
    EnumTickStatus,
    ModelTickRequest,
    ModelTickResult,
    ModelTickSnapshot,
)

pytestmark = pytest.mark.unit

REPLAY = Path(__file__).resolve().parents[2] / "fixtures" / "pr_landing_tick_replay"
FIXTURES = sorted(REPLAY.glob("*.json"))
NODE_TICK_OFFSET = 1_000_000
# A recorded tick that cannot be made equal is named here with its reason; none is skipped silently.
KNOWN_DIVERGENT: dict[str, str] = {}


def _controller_facts(path: Path) -> ModelLandingFacts:
    return ModelTickSnapshot.model_validate_json(path.read_text(encoding="utf-8")).facts


def _watcher_shaped(facts: ModelLandingFacts) -> ModelLandingFacts:
    """What the PR watcher's state gives: no controller state, every carried field at its default."""
    prs = []
    for p in facts.prs:
        reset = {
            name: type(p).model_fields[name].get_default(call_default_factory=True)
            for name in CARRIED_PR_FIELDS
        }
        # the watcher's state carries no classification: a red head is given the one declared class
        reset["red_class"] = (
            EnumLandingRedClass.PRODUCT if p.ci is EnumLandingCi.RED else None
        )
        prs.append(p.model_copy(update=reset))
    values: dict[str, object] = {
        name: getattr(facts, name) for name in ModelLandingFacts.model_fields
    }
    for name in CARRIED_FACT_FIELDS:
        field = ModelLandingFacts.model_fields[name]
        values[name] = field.get_default(call_default_factory=True)
    values.update(
        tick=facts.tick + NODE_TICK_OFFSET,
        prs=tuple(prs),
        companions=(),
        state=ModelLandingControllerState(),
    )
    return ModelLandingFacts.model_validate(values)


def _compare(path: Path, *, carried: bool) -> tuple[ModelTickResult, str]:
    facts = _controller_facts(path)
    scope = facts.prs[0].pr.partition("#")[0]
    carry = ModelLandingControllerCarry(
        controller_tick=facts.tick,
        observed_at=facts.observed_at,
        facts=facts,
        source="replay",
    )
    node = _watcher_shaped(facts)
    if carried:
        node = carry_controller_state(node, carry)
    result = HandlerPrLandingTickOrchestrator(cutover_repository=scope).handle(
        ModelTickRequest(
            correlation_id=uuid4(),
            mode=EnumTickMode.OBSERVE,
            facts=node,
            controller=carry,
        )
    )
    return result, scope


def test_at_least_thirty_recorded_ticks_are_replayed() -> None:
    assert len(FIXTURES) >= 30


@pytest.mark.parametrize("path", FIXTURES, ids=lambda p: p.stem)
def test_the_carried_tick_agrees_with_the_controller_for_its_repository(
    path: Path,
) -> None:
    result, scope = _compare(path, carried=True)
    assert result.status is EnumTickStatus.COMPARED, result.failure_detail
    if path.name in KNOWN_DIVERGENT:
        assert all(d.subject.startswith(f"{scope}#") for d in result.divergences)
        return
    assert result.equal, [(d.subject, d.differences) for d in result.divergences]
    assert result.agreed_prs == result.compared_prs


def test_failing_control_the_same_ticks_without_the_carry_diverge() -> None:
    results = [_compare(path, carried=False)[0] for path in FIXTURES]
    assert all(r.status is EnumTickStatus.COMPARED for r in results), [
        r.failure_detail for r in results if r.status is not EnumTickStatus.COMPARED
    ]
    diverged = [
        path.stem for path, r in zip(FIXTURES, results, strict=True) if not r.equal
    ]
    assert diverged, (
        "no recorded tick depends on the carried state: the replay cannot fail"
    )
