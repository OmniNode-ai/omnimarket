# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The committed R7 inputs pass their own schema test (OMN-23696)."""

from __future__ import annotations

import pytest
import yaml

from omnimarket.nodes.node_delegation_scenario_r7_effect.handlers.handler_scenario_r7 import (
    CONTRACT_PATH,
    NODE_DIR,
    ModelR7Inputs,
    load_inputs,
    load_runtime,
)

pytestmark = pytest.mark.unit

INPUTS = NODE_DIR / "inputs" / "r7_legs.yaml"


def test_r7_inputs_parse_into_the_typed_model() -> None:
    inputs = load_inputs(INPUTS)
    assert inputs.task_type == "summarization"
    assert inputs.inmemory_host == "localhost"
    assert inputs.failing_rung.backend_id == "r7-failing"
    assert inputs.answering_rung.backend_id == "r7-answering"


def test_r7_inputs_reject_an_unknown_key() -> None:
    raw = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    raw["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        ModelR7Inputs.model_validate(raw)


def test_r7_two_rungs_and_the_first_is_free_the_second_paid() -> None:
    inputs = load_inputs(INPUTS)
    assert inputs.failing_rung.behaviour == "status"
    assert inputs.failing_rung.status_code == 503
    assert inputs.failing_rung.cost_class == "free"
    assert inputs.answering_rung.behaviour == "answer"
    assert inputs.answering_rung.cost_class == "paid"
    assert inputs.answering_rung.tier == "cloud"
    assert inputs.answering_rung.scripted_cost_usd > 0


def test_r7_the_backend_ids_differ() -> None:
    inputs = load_inputs(INPUTS)
    assert inputs.failing_rung.backend_id != inputs.answering_rung.backend_id


def test_r7_answer_carries_the_marker() -> None:
    inputs = load_inputs(INPUTS)
    assert inputs.answer_marker in inputs.answer


def test_r7_contract_runtime_block_is_typed_and_names_the_inputs_file() -> None:
    runtime = load_runtime(CONTRACT_PATH)
    assert runtime.scenario_id == "R7"
    assert (NODE_DIR / runtime.inputs_relpath) == INPUTS
