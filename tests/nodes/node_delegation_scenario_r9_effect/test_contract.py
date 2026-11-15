# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The committed contract and inputs of scenario R9 (OMN-24554) pass their own schema.

The runtime block must validate as the shared readiness runtime model (it is the block every scenario node declares),
the bus block must name a command node that is really there, the inputs file must hold exactly the committed corpus
cases, and the schedule must install the R9 unit.
"""

from __future__ import annotations

from importlib import resources

import pytest
import yaml

from omnimarket.handlers.delegation_readiness_scenario import ModelReadinessRuntime
from omnimarket.models.model_delegation_scenario import EnumScenarioCadence
from omnimarket.nodes.node_delegation_scenario_r9_effect.handlers.handler_scenario_r9 import (
    CONTRACT_PATH,
    NODE_DIR,
    ModelR9Inputs,
    load_bus_settings,
    load_inputs,
    load_runtime,
    load_schedule,
)

pytestmark = pytest.mark.unit

INPUTS = NODE_DIR / "inputs/r9_run.yaml"
CASE_IDS = ["E1", "E2", "E3", "E4", "E5", "E6", "E11"]


def test_the_runtime_block_validates_as_the_shared_runtime_model() -> None:
    raw = yaml.safe_load(CONTRACT_PATH.read_text(encoding="utf-8"))
    runtime = ModelReadinessRuntime.model_validate(raw["runtime"])
    assert runtime == load_runtime()
    assert runtime.cadence is EnumScenarioCadence.HOURLY
    assert runtime.scenario_id == "R9"
    assert runtime.lane == "node_delegation_scenario_r9"
    assert (NODE_DIR / runtime.inputs_relpath) == INPUTS


def test_the_bus_block_names_an_importable_command_node() -> None:
    settings = load_bus_settings()
    contract = resources.files(settings.command_node).joinpath("contract.yaml")
    assert contract.is_file()


def test_the_committed_inputs_validate_and_name_every_case() -> None:
    inputs = load_inputs(INPUTS)
    assert [case.id for case in inputs.cases] == CASE_IDS


def test_a_duplicate_case_id_is_refused() -> None:
    raw = yaml.safe_load(INPUTS.read_text(encoding="utf-8"))
    raw["cases"] = [raw["cases"][0], raw["cases"][0]]
    with pytest.raises(ValueError, match="not unique"):
        ModelR9Inputs.model_validate(raw)


def test_the_schedule_installs_the_r9_unit() -> None:
    assert load_schedule().unit_name == "onex-delegation-scenario-r9"
