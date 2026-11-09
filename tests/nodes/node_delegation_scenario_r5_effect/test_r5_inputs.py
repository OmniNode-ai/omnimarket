# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 of OMN-23183: the committed R5 inputs and the contract's runtime block pass their own schema test."""

from __future__ import annotations

import pytest
import yaml

from omnimarket.models.delegation.wire.model_probe_faults import ModelProbeFaults
from omnimarket.nodes.node_delegation_scenario_r5_effect.handlers.handler_scenario_r5 import (
    CONTRACT_PATH,
    NODE_DIR,
    ModelR5Inputs,
    load_inputs,
    load_runtime,
    load_schedule,
)

pytestmark = pytest.mark.unit

INPUTS = NODE_DIR / "inputs" / "r5_legs.yaml"
DAYS_CONTRACT = (
    NODE_DIR.parent / "node_delegation_scenario_days_effect" / "contract.yaml"
)


def raw() -> dict[str, object]:
    loaded = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def test_r5_inputs_parse_into_the_typed_model() -> None:
    inputs = load_inputs(INPUTS)
    assert inputs.provider == "openrouter"
    assert inputs.model_wire_key == "model"
    assert inputs.fault_status_code == 429
    assert inputs.answer_marker in inputs.answer


def test_r5_inputs_reject_an_unknown_key() -> None:
    changed = raw()
    changed["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        ModelR5Inputs.model_validate(changed)


def test_r5_the_two_models_are_free_distinct_and_listed_for_the_key() -> None:
    inputs = load_inputs(INPUTS)
    assert inputs.model_a != inputs.model_b
    assert inputs.model_a.endswith(":free")
    assert inputs.model_b.endswith(":free")
    assert {inputs.model_a, inputs.model_b} <= set(inputs.key_listed_models)
    assert inputs.unlisted_model not in inputs.key_listed_models


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("model_b", "google/gemma-4-31b-it:free", "different"),
        ("unlisted_model", "qwen/qwen3-coder:free", "must not be in"),
        ("model_a", "example/missing:free", "not in the key's listed"),
    ],
)
def test_r5_inputs_reject_an_incoherent_model_set(
    field: str, value: str, message: str
) -> None:
    changed = raw()
    changed[field] = value
    with pytest.raises(ValueError, match=message):
        ModelR5Inputs.model_validate(changed)


def test_r5_the_faults_file_names_model_a_only_and_is_a_valid_faults_ladder() -> None:
    inputs = load_inputs(INPUTS)
    faults = inputs.faults()
    assert ModelProbeFaults.model_validate(faults.model_dump(mode="json")) == faults
    assert faults.fault_for(inputs.model_a) is not None
    assert faults.fault_for(inputs.model_b) is None
    assert faults.faults[0].failure_class.value == inputs.rate_limited_class


def test_r5_contract_runtime_block_is_typed_and_names_the_inputs_file_and_key_variable() -> (
    None
):
    runtime = load_runtime(CONTRACT_PATH)
    assert runtime.scenario_id == "R5"
    assert runtime.cadence.value == "hourly"
    assert runtime.leg_backend == "inmemory"
    assert runtime.model_backend == "inmemory"
    assert runtime.openrouter_probe_key_env.isupper()
    assert (NODE_DIR / runtime.inputs_relpath) == INPUTS


def test_r5_lane_and_cadence_are_the_ones_the_green_days_reader_names() -> None:
    days = yaml.safe_load(DAYS_CONTRACT.read_text(encoding="utf-8"))
    scenarios = {item["scenario_id"]: item for item in _scenarios(days)}
    runtime = load_runtime(CONTRACT_PATH)
    assert scenarios["R5"]["lane"] == runtime.lane == "node_delegation_scenario_r5"
    assert scenarios["R5"]["cadence"] == runtime.cadence.value


def test_r5_schedule_is_the_hourly_minute_05_timer_with_the_key_in_an_environment_file() -> (
    None
):
    schedule = load_schedule(CONTRACT_PATH)
    assert schedule.cadence.on_calendar == "*-*-* *:05:00"
    assert schedule.environment_file is not None
    assert load_runtime(CONTRACT_PATH).openrouter_probe_key_env not in str(
        schedule.unit_environment
    )


def _scenarios(node: object) -> list[dict[str, str]]:
    """Every mapping in the days contract that carries a scenario id."""
    found: list[dict[str, str]] = []
    if isinstance(node, dict):
        if "scenario_id" in node and "lane" in node:
            found.append(node)
        for item in node.values():
            found.extend(_scenarios(item))
    elif isinstance(node, list):
        for item in node:
            found.extend(_scenarios(item))
    return found
