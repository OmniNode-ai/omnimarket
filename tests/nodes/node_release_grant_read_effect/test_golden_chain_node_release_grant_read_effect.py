# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Registered package -> contract -> routed handlers -> typed results (OMN-21493)."""

from __future__ import annotations

import importlib
from importlib.metadata import entry_points
from importlib.resources import files
from pathlib import Path

import pytest
import yaml

from omnimarket.models.release_gate.model_release_grant_facts import EnumGrantState
from omnimarket.nodes.node_release_action_classify_compute.models.model_release_action import (
    EnumReleaseDecision,
)
from omnimarket.nodes.node_release_grant_read_effect import (
    HandlerCheckReleaseGate,
    HandlerReadReleaseGrant,
)
from omnimarket.nodes.node_release_grant_read_effect.models.model_release_grant import (
    ModelCheckReleaseGateResult,
)

from . import release_gate_fixtures as f

pytestmark = pytest.mark.unit

NODE = "node_release_grant_read_effect"
TERMINAL_EVENT = "onex.evt.omnimarket.release-grant-read.v1"
COMMAND_TOPIC = "onex.cmd.omnimarket.release-grant-read-requested.v1"


def _contract() -> dict[str, object]:
    entries = [e for e in entry_points(group="onex.nodes") if e.name == NODE]
    assert len(entries) == 1
    assert entries[0].value == f"omnimarket.nodes.{NODE}"
    package = entries[0].load()
    loaded = yaml.safe_load(
        Path(str(files(package).joinpath("contract.yaml"))).read_text(encoding="utf-8")
    )
    assert isinstance(loaded, dict)
    return loaded


def test_registered_contract_routes_each_operation_to_its_typed_handler() -> None:
    contract = _contract()
    assert contract["node_type"] == "effect"
    routes = contract["handler_routing"]
    assert isinstance(routes, dict)
    operations = {h["operation"]: h for h in routes["handlers"]}
    assert set(operations) == {
        "read_release_grant",
        "record_authorization_receipt",
        "check_release_gate",
    }
    for route in operations.values():
        module = importlib.import_module(route["handler"]["module"])
        assert hasattr(module, route["handler"]["name"])
        input_module, input_name = route["input_model"].rsplit(".", 1)
        assert hasattr(importlib.import_module(input_module), input_name)
        output_module, output_name = route["output_model"].rsplit(".", 1)
        assert hasattr(importlib.import_module(output_module), output_name)


def test_contract_command_and_terminal_event_are_the_declared_topics() -> None:
    contract = _contract()
    assert contract["terminal_event"] == TERMINAL_EVENT
    assert contract["event_bus"] == {
        "subscribe_topics": [COMMAND_TOPIC],
        "publish_topics": [TERMINAL_EVENT],
    }
    assert contract["runtime_dispatch"] == {
        "command_topic": COMMAND_TOPIC,
        "terminal_events": {"success": TERMINAL_EVENT},
    }


def test_a_granted_release_pull_request_passes_the_whole_chain() -> None:
    source, ledger = f.HistorySource(f.standing_grant()), f.RecordingLedger()
    reader = HandlerReadReleaseGrant(source, ledger, f.FixedClock())
    checker = HandlerCheckReleaseGate(reader, lambda _head: f.policy())
    result = checker.handle(f.check_request())
    assert isinstance(result, ModelCheckReleaseGateResult)
    assert result.exit_code == 0
    assert result.decision.decision is EnumReleaseDecision.ADMIT
    assert result.grant is not None
    assert result.grant.state is EnumGrantState.GRANTED
    assert [kind for kind, _ in ledger.rows] == ["release_gate_decision"]
