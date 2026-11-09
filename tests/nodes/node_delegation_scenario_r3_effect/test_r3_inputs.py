# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 of OMN-23182: the committed R3 ladder file passes its own schema test."""

from __future__ import annotations

import pytest
import yaml

from omnimarket.enums.enum_delegation_end_of_ladder_reason import (
    EnumDelegationEndOfLadderReason,
)
from omnimarket.enums.enum_delegation_failure_class import EnumDelegationFailureClass
from omnimarket.handlers.delegation_readiness_probe import ScriptedLegTransport
from omnimarket.models.delegation.wire.model_probe_ladder import (
    ModelProbeLadder,
    ModelProbeRung,
)
from omnimarket.nodes.node_delegation_scenario_r3_effect.handlers.handler_scenario_r3 import (
    CONTRACT_PATH,
    NODE_DIR,
    ModelR3Inputs,
    load_inputs,
    load_runtime,
    load_schedule,
)

pytestmark = pytest.mark.unit

INPUTS = NODE_DIR / "inputs" / "r3_legs.yaml"
#: the lane node_delegation_scenario_days_effect lists for R3, so the green-days reader finds this node's rows
DAYS_LANE = "node_delegation_scenario_r3"


def test_r3_inputs_parse_into_the_typed_model() -> None:
    inputs = load_inputs(INPUTS)
    assert inputs.leg_a.name == "free_503_then_paid_stub"
    assert inputs.leg_b.name == "free_503_then_real_paid"
    assert inputs.all_down_leg.name == "free_and_paid_both_503"


def test_r3_inputs_reject_an_unknown_key() -> None:
    raw = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    raw["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        ModelR3Inputs.model_validate(raw)


def test_r3_every_leg_ladder_is_a_valid_probe_ladder() -> None:
    inputs = load_inputs(INPUTS)
    for leg in (inputs.leg_a, inputs.leg_b, inputs.all_down_leg):
        spec = inputs.leg_spec(leg.name, leg.rungs)
        urls = ScriptedLegTransport().endpoints(spec)
        ModelProbeLadder(
            rungs=tuple(
                ModelProbeRung(
                    backend_id=rung.backend_id,
                    model_id=rung.model_id,
                    endpoint_ref=urls[rung.backend_id],
                    tier=rung.tier,
                    timeout_ms=rung.timeout_ms,
                )
                for rung in spec.rungs
            )
        )


def test_r3_first_rung_is_a_free_503_and_second_is_paid_on_every_escalation_leg() -> (
    None
):
    inputs = load_inputs(INPUTS)
    rule = inputs.free_rung
    assert rule.expect_failure_class in {c.value for c in EnumDelegationFailureClass}
    assert rule.expect_failure_class == "model_unavailable"
    for leg in (inputs.leg_a, inputs.leg_b):
        free, paid = leg.rungs
        assert (free.behaviour, free.status_code, free.cost_class) == (
            "status",
            503,
            "free",
        ), leg.name
        assert paid.behaviour == "answer", leg.name
        assert paid.cost_class == "paid", leg.name
        assert paid.tier in inputs.paid_tiers, leg.name
        assert free.tier not in inputs.paid_tiers, leg.name
        assert paid.scripted_cost_usd > 0


def test_r3_leg_b_rung_names_the_real_backend_and_caps_tokens_at_sixteen() -> None:
    inputs = load_inputs(INPUTS)
    real = next(
        r for r in inputs.leg_b.rungs if r.backend_id == inputs.leg_b.real_backend_id
    )
    assert real is inputs.leg_b.rungs[1]
    assert real.max_tokens == 16
    assert load_runtime(CONTRACT_PATH).openrouter_max_tokens == 16


def test_r3_control_ladder_has_both_rungs_answering_503() -> None:
    inputs = load_inputs(INPUTS)
    assert [(r.behaviour, r.status_code) for r in inputs.all_down_leg.rungs] == [
        ("status", 503),
        ("status", 503),
    ]
    assert set(inputs.end_of_ladder_reasons) == {
        r.value for r in EnumDelegationEndOfLadderReason
    }


def test_r3_prompt_is_a_large_input_and_answer_carries_the_marker() -> None:
    inputs = load_inputs(INPUTS)
    assert inputs.expect_size_band == "large"
    assert inputs.answer_marker in inputs.answer
    assert inputs.answer.strip().endswith(inputs.expected_returned.strip())
    assert sum(line.startswith("- ") for line in inputs.prompt.splitlines()) > 10


def test_r3_contract_runtime_block_is_typed_names_the_days_lane_and_the_inputs_file() -> (
    None
):
    runtime = load_runtime(CONTRACT_PATH)
    assert runtime.scenario_id == "R3"
    assert runtime.cadence.value == "every_6h"
    assert runtime.lane == DAYS_LANE
    assert (NODE_DIR / runtime.inputs_relpath) == INPUTS
    assert runtime.leg_b_enabled is True
    assert runtime.openrouter_probe_key_env
    assert runtime.openrouter_base_url.startswith("https://")


def test_r3_probe_key_is_named_never_valued_in_the_contract_or_the_inputs() -> None:
    for path in (CONTRACT_PATH, INPUTS):
        text = path.read_text(encoding="utf-8")
        assert "sk-" not in text, path.name
    schedule = load_schedule(CONTRACT_PATH)
    assert load_runtime(CONTRACT_PATH).openrouter_probe_key_env not in (
        schedule.unit_environment
    )
    assert schedule.environment_file is not None
