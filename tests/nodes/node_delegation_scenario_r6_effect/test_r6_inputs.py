# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 of OMN-23174: the committed R6 inputs pass their own schema test."""

from __future__ import annotations

import pytest
import yaml

from omnimarket.handlers.delegation_readiness_probe import ScriptedLegTransport
from omnimarket.models.delegation.model_execution_budget_overlay import (
    ModelExecutionBudgetOverlay,
)
from omnimarket.models.delegation.wire.model_probe_ladder import (
    ModelProbeLadder,
    ModelProbeRung,
)
from omnimarket.nodes.node_delegation_scenario_r6_effect.handlers.handler_scenario_r6 import (
    CONTRACT_PATH,
    NODE_DIR,
    ModelR6Inputs,
    load_inputs,
    load_runtime,
)

pytestmark = pytest.mark.unit

INPUTS = NODE_DIR / "inputs" / "r6_legs.yaml"


def test_r6_inputs_parse_into_the_typed_model() -> None:
    inputs = load_inputs(INPUTS)
    assert inputs.task_type == "summarization"
    assert inputs.overlay_ceiling_seconds == 600
    assert inputs.contract_ceiling_seconds == 240


def test_r6_inputs_reject_an_unknown_key() -> None:
    raw = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    raw["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        ModelR6Inputs.model_validate(raw)


def test_r6_the_rung_answers_after_300_s_inside_its_own_450_s_deadline() -> None:
    rung = load_inputs(INPUTS).rung
    assert rung.behaviour == "slow"
    assert rung.delay_ms == 300000
    assert rung.timeout_ms == 450000
    assert rung.delay_ms < rung.timeout_ms


def test_r6_the_request_sits_between_the_contract_ceiling_and_the_overlay_ceiling() -> (
    None
):
    inputs = load_inputs(INPUTS)
    assert (
        inputs.contract_ceiling_seconds
        < inputs.requested_timeout_seconds
        < inputs.overlay_ceiling_seconds
    )
    assert inputs.rung.delay_ms > inputs.contract_ceiling_seconds * 1000
    assert inputs.rung.timeout_ms < inputs.overlay_ceiling_seconds * 1000


def test_r6_the_overlay_document_validates_and_sets_only_the_class_ceiling() -> None:
    inputs = load_inputs(INPUTS)
    overlay = inputs.overlay()
    assert ModelExecutionBudgetOverlay.model_validate(overlay.model_dump()) == overlay
    override = overlay.execution_budgets[inputs.task_type]
    assert override.task_class_timeout_ceiling_seconds == 600
    assert override.terminal_delivery_margin_seconds is None


def test_r6_both_legs_build_a_valid_probe_ladder_and_only_the_first_has_the_overlay() -> (
    None
):
    inputs = load_inputs(INPUTS)
    long = inputs.leg_spec("overlay_long_task", with_overlay=True)
    refused = inputs.leg_spec("no_overlay_refused", with_overlay=False)
    assert long.budget_overlay is not None
    assert refused.budget_overlay is None
    for leg in (long, refused):
        assert leg.isolate_overlay_root
        urls = ScriptedLegTransport().endpoints(leg)
        ModelProbeLadder(
            rungs=tuple(
                ModelProbeRung(
                    backend_id=rung.backend_id,
                    model_id=rung.model_id,
                    endpoint_ref=urls[rung.backend_id],
                    timeout_ms=rung.timeout_ms,
                )
                for rung in leg.rungs
            )
        )


def test_r6_answer_carries_the_marker() -> None:
    inputs = load_inputs(INPUTS)
    assert inputs.answer_marker in inputs.answer


def test_r6_contract_runtime_block_is_typed_and_names_the_inputs_file() -> None:
    runtime = load_runtime(CONTRACT_PATH)
    assert runtime.scenario_id == "R6"
    assert runtime.cadence.value == "daily"
    assert (NODE_DIR / runtime.inputs_relpath) == INPUTS
