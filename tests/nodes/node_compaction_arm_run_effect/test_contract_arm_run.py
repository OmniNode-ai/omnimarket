# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract topics, operation routing and the six-arm adapter roster."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

import omnimarket.nodes.node_compaction_arm_run_effect as node_package
from omnimarket.nodes.node_compaction_arm_run_effect.handlers.handler_arm_run import (
    HandlerCompactionArmRun,
    load_arm_roster,
)
from omnimarket.nodes.node_compaction_arm_run_effect.models.model_arm_run import (
    EnumArm,
    EnumArmTrack,
    ModelArmAdapterSpec,
    ModelArmRunRequest,
    ModelArmRunResult,
)

pytestmark = pytest.mark.unit

TERMINAL = "onex.evt.omnimarket.compaction-arm-run-completed.v1"
COMMAND = "onex.cmd.omnimarket.compaction-arm-run-start.v1"


def _contract() -> dict[str, object]:
    assert node_package.__file__ is not None
    path = Path(node_package.__file__).parent / "contract.yaml"
    loaded = yaml.safe_load(path.read_text())
    assert isinstance(loaded, dict)
    return loaded


def test_terminal_event_is_the_published_topic() -> None:
    contract = _contract()
    bus = contract["event_bus"]
    assert isinstance(bus, dict)
    assert contract["terminal_event"] == TERMINAL
    assert bus["publish_topics"] == [TERMINAL]
    assert bus["subscribe_topics"] == [COMMAND]
    dispatch = contract["runtime_dispatch"]
    assert isinstance(dispatch, dict)
    assert dispatch["command_topic"] == COMMAND
    assert contract["externally_consumed_topics"] == [TERMINAL]


def test_run_compaction_arms_routed_handler_and_models_resolve() -> None:
    contract = _contract()
    routing = contract["handler_routing"]
    assert isinstance(routing, dict)
    assert routing["routing_strategy"] == "operation_match"
    entries = routing["handlers"]
    assert isinstance(entries, list)
    assert len(entries) == 1
    entry = entries[0]
    assert isinstance(entry, dict)
    assert entry["operation"] == "run_compaction_arms"
    handler_spec = entry["handler"]
    assert isinstance(handler_spec, dict)
    module = importlib.import_module(handler_spec["module"])
    handler = getattr(module, handler_spec["name"])
    assert handler is HandlerCompactionArmRun
    assert callable(handler().handle)
    for field, expected in (
        ("input_model", ModelArmRunRequest),
        ("output_model", ModelArmRunResult),
    ):
        model = entry[field]
        assert isinstance(model, str)
        module_name, _, class_name = model.rpartition(".")
        assert getattr(importlib.import_module(module_name), class_name) is expected
    top_level = contract["handler"]
    assert isinstance(top_level, dict)
    assert top_level["module"] == handler_spec["module"]
    assert top_level["class"] == handler_spec["name"]


def test_loaded_roster_matches_contract_and_exactly_six_arms() -> None:
    rows = _contract()["arm_adapters"]
    assert isinstance(rows, list)
    expected = tuple(ModelArmAdapterSpec.model_validate(row) for row in rows)
    roster = load_arm_roster()
    assert isinstance(roster, tuple)
    assert roster == expected
    assert len(roster) == 6
    assert {spec.arm.value for spec in roster} == {
        "stock_rerun",
        "mod",
        "stock_recorded",
        "oracle",
        "empty",
        "inverted",
    }
    by_arm = {spec.arm: spec for spec in roster}
    assert len(by_arm) == len(roster)
    for arm in (
        EnumArm.ORACLE,
        EnumArm.EMPTY,
        EnumArm.INVERTED,
        EnumArm.STOCK_RECORDED,
    ):
        assert by_arm[arm].calls_model is False
    for arm in (EnumArm.STOCK_RERUN, EnumArm.MOD):
        assert by_arm[arm].calls_model is True
    assert by_arm[EnumArm.STOCK_RERUN].track is EnumArmTrack.STOCK
    assert by_arm[EnumArm.STOCK_RECORDED].track is EnumArmTrack.STOCK
    assert by_arm[EnumArm.MOD].track is EnumArmTrack.MOD
    for arm in (EnumArm.ORACLE, EnumArm.EMPTY, EnumArm.INVERTED):
        assert by_arm[arm].track is EnumArmTrack.CONTROL
