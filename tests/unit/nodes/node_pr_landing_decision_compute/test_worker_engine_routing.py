# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The fix worker's engine follows the lane routing policy and is never Codex (OMN-23708).

Recorded 2026-11-11: the first live observe run's decision node dispatched omnimemory pull request 713's fix
worker on ``codex_high``. The operator ruled on 2026-11-05 (RULING 2026-11-05T15:07:26Z) that Codex reviews
plans and is no longer an engine for lane work; the old controller's ladder is Sonnet. The node's worker
ladder now comes from the routing policy's own order (``node_lane_model_policy_compute``): only its Claude
models are rungs, Opus only on a declared reason. Each test names the break that turns it red.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from omnimarket.nodes.node_lane_model_policy_compute.handlers import (
    handler_lane_model_policy as routing,
)
from omnimarket.nodes.node_pr_landing_decision_compute import decide_landing
from omnimarket.nodes.node_pr_landing_decision_compute.models import (
    model_landing_facts as facts_model,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.enum_landing import (
    EnumLandingActionKind,
    EnumLandingEngine,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
    ModelLandingPolicy,
)
from tests.unit.nodes.node_pr_landing_decision_compute.test_external_judgement_gate import (
    HELD,
    TICK_TIMES,
    _facts,
)

pytestmark = pytest.mark.unit


def _engines(facts: ModelLandingFacts) -> list[EnumLandingEngine]:
    return [
        a.brief.engine
        for a in decide_landing(facts).actions
        if a.kind is EnumLandingActionKind.DISPATCH_WORKER
        and a.subject == HELD
        and a.brief is not None
    ]


@pytest.mark.parametrize("at", TICK_TIMES)
def test_the_recorded_713_ticks_dispatch_on_sonnet_never_codex(at: datetime) -> None:
    # failing control: the ladder built from the policy's order put Codex first, so this read codex_high
    assert _engines(_facts(at)) == [EnumLandingEngine.CLAUDE_SONNET]


def test_the_default_ladders_hold_no_codex_rung() -> None:
    policy = ModelLandingPolicy()
    assert EnumLandingEngine.CODEX_HIGH not in policy.engine_ladder
    assert EnumLandingEngine.CODEX_HIGH not in policy.cause_engine_ladder
    assert set(policy.engine_ladder) == {EnumLandingEngine.CLAUDE_SONNET}


def test_a_codex_rung_a_controller_carried_is_skipped_for_the_next_rung() -> None:
    carried = ModelLandingPolicy(
        engine_ladder=(EnumLandingEngine.CODEX_HIGH, EnumLandingEngine.CLAUDE_SONNET)
    )
    assert _engines(_facts(TICK_TIMES[0], policy=carried)) == [
        EnumLandingEngine.CLAUDE_SONNET
    ]


def test_a_pool_that_offers_only_codex_dispatches_nothing() -> None:
    codex_only = _facts(TICK_TIMES[0]).model_copy(
        update={"available_engines": (EnumLandingEngine.CODEX_HIGH,)}
    )
    assert _engines(codex_only) == []


def test_the_ladder_follows_the_routing_policys_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declared = routing.load_policy()
    # a policy whose ceiling is Opus offers the Opus rung after Sonnet, in the policy's own order
    opened = declared.model_copy(update={"ceiling": "opus", "escalation_models": ()})
    monkeypatch.setattr(facts_model, "load_policy", lambda: opened)

    def no_refusal(_model: str, **_facts: object) -> None:
        return None

    monkeypatch.setattr(facts_model, "lane_model_refusal", no_refusal)
    assert facts_model._worker_ladder(3) == (
        EnumLandingEngine.CLAUDE_SONNET,
        EnumLandingEngine.CLAUDE_OPUS,
        EnumLandingEngine.CLAUDE_OPUS,
    )
    # the shipped policy, as the lanes use it: Opus needs a declared complexity reason, so Sonnet only
    monkeypatch.undo()
    assert facts_model._worker_ladder(3) == (EnumLandingEngine.CLAUDE_SONNET,) * 3
