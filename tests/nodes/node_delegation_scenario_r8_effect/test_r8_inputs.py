# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""AC1 of OMN-23175: the committed R8 inputs pass their own schema test."""

from __future__ import annotations

from decimal import Decimal

import pytest
import yaml

from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.adapter_r8_lane import (
    RUN_NAMES,
    TOTALS_NAMES,
)
from omnimarket.nodes.node_delegation_scenario_r8_effect.handlers.handler_scenario_r8 import (
    CONTRACT_PATH,
    NODE_DIR,
    ModelR8Inputs,
    load_inputs,
    load_lane_settings,
    load_runtime,
)

pytestmark = pytest.mark.unit

INPUTS = NODE_DIR / "inputs" / "r8_run.yaml"


def test_r8_inputs_parse_into_the_typed_model() -> None:
    inputs = load_inputs(INPUTS)
    assert inputs.synthetic.traffic_class == "synthetic"
    assert inputs.synthetic.source_surface == "readiness_probe"
    assert inputs.organic.traffic_class == "organic"
    assert inputs.organic.source_surface is None


def test_r8_inputs_reject_an_unknown_key() -> None:
    raw = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    raw["surprise"] = 1
    with pytest.raises(ValueError, match="surprise"):
        ModelR8Inputs.model_validate(raw)


def test_r8_the_synthetic_request_names_its_surface_and_the_organic_one_names_neither() -> (
    None
):
    inputs = load_inputs(INPUTS)
    synthetic = inputs.request(inputs.synthetic)
    organic = inputs.organic_request()
    assert (synthetic.traffic_class, synthetic.source_surface) == (
        "synthetic",
        "readiness_probe",
    )
    assert (organic.traffic_class, organic.source_surface) == (None, None)
    assert synthetic.prompt == organic.prompt
    assert synthetic.task_type == organic.task_type


def test_r8_wait_is_longer_than_one_poll_and_money_has_a_quantum() -> None:
    inputs = load_inputs(INPUTS)
    assert inputs.wait_timeout_seconds > inputs.poll_interval_seconds
    assert inputs.quantum() == Decimal("0.000001")


def test_r8_the_contract_runtime_block_is_typed_and_names_the_inputs_file() -> None:
    runtime = load_runtime(CONTRACT_PATH)
    assert runtime.scenario_id == "R8"
    assert runtime.cadence.value == "every_6h"
    assert (NODE_DIR / runtime.inputs_relpath) == INPUTS


def test_r8_the_lane_block_names_every_total_and_every_run_field() -> None:
    settings = load_lane_settings(CONTRACT_PATH)
    assert settings.backend == "http"
    assert set(settings.totals_keys) == set(TOTALS_NAMES)
    assert set(settings.run_keys) == set(RUN_NAMES)
