# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Determinism and replay of the landing decision.

``decide`` has no clock, no network and no model inside it: the same facts give
byte-identical decisions, in any input order, and replaying a recorded
scenario gives the same decision stream every time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_pr_landing_decision_compute.handlers.handler_pr_landing_decision import (
    decide_landing,
)
from omnimarket.nodes.node_pr_landing_decision_compute.models.model_landing_facts import (
    ModelLandingFacts,
)
from tests.unit.nodes.node_pr_landing_decision_compute.landing_world import run_scenario

FIXTURES = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "pr_landing_decision"
    / "review_scenarios"
)
SPECS = [yaml.safe_load(p.read_text()) for p in sorted(FIXTURES.glob("*.yaml"))]


def _facts_stream(spec: dict[str, Any]) -> list[ModelLandingFacts]:
    return run_scenario(spec).facts_seen


def _reversed(facts: ModelLandingFacts) -> ModelLandingFacts:
    state = facts.state
    return facts.model_copy(
        update={
            "prs": tuple(reversed(facts.prs)),
            "companions": tuple(reversed(facts.companions)),
            "probes": tuple(reversed(facts.probes)),
            "results": tuple(reversed(facts.results)),
            "rebuild_acks": tuple(reversed(facts.rebuild_acks)),
            "producer_failures": tuple(reversed(facts.producer_failures)),
            "state": state.model_copy(
                update={
                    "leases": tuple(reversed(state.leases)),
                    "records": tuple(reversed(state.records)),
                    "rebuilds": tuple(reversed(state.rebuilds)),
                    "uncovered": tuple(reversed(state.uncovered)),
                    "eligibility_reruns": tuple(reversed(state.eligibility_reruns)),
                }
            ),
        }
    )


@pytest.mark.unit
@pytest.mark.parametrize("spec", SPECS, ids=[s["id"] for s in SPECS])
def test_determinism(spec: dict[str, Any]) -> None:
    """decide run twice on the same snapshot gives byte-identical decisions."""
    for facts in _facts_stream(spec):
        first = decide_landing(facts).model_dump_json()
        assert decide_landing(facts).model_dump_json() == first
        assert (
            decide_landing(
                ModelLandingFacts.model_validate_json(facts.model_dump_json())
            ).model_dump_json()
            == first
        )


@pytest.mark.unit
@pytest.mark.parametrize("spec", SPECS, ids=[s["id"] for s in SPECS])
def test_input_order_does_not_matter(spec: dict[str, Any]) -> None:
    for facts in _facts_stream(spec):
        assert (
            decide_landing(_reversed(facts)).model_dump_json()
            == decide_landing(facts).model_dump_json()
        )


@pytest.mark.unit
@pytest.mark.parametrize("spec", SPECS, ids=[s["id"] for s in SPECS])
def test_replay_gives_the_same_decision_stream(spec: dict[str, Any]) -> None:
    first = [d.model_dump_json() for d in run_scenario(spec).decisions]
    second = [d.model_dump_json() for d in run_scenario(spec).decisions]
    assert first == second
