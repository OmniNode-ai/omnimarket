# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain: the packaged contract resolves and the judge answers a fire that filled the lab (OMN-24109)."""

from __future__ import annotations

import importlib
from importlib.resources import files
from typing import Any

import pytest
import yaml

from omnimarket.nodes.node_lab_fill_fire_outcome_compute.models import (
    EnumLabFillResultChannel,
    ModelLabFillFireOutcomePolicy,
    ModelLabFillFireOutcomeResult,
)

from .conftest import make_request

pytestmark = pytest.mark.unit

NAME = "node_lab_fill_fire_outcome_compute"


def _contract() -> dict[str, Any]:
    text = (
        files(f"omnimarket.nodes.{NAME}")
        .joinpath("contract.yaml")
        .read_text(encoding="utf-8")
    )
    document: dict[str, Any] = yaml.safe_load(text)
    return document


def test_golden_chain_contract_is_in_process_only() -> None:
    """The node is called in process: no command topic, no subscription, no runtime profile (no new broker topic)."""
    contract = _contract()
    assert contract["name"] == NAME
    assert contract["node_type"] == "COMPUTE_GENERIC"
    assert contract["event_bus"] == {"subscribe_topics": [], "publish_topics": []}
    assert contract["runtime_profiles"] == []
    assert "command_topic" not in contract["runtime_dispatch"]
    assert (
        contract["terminal_event"]
        == "onex.evt.omnimarket.lab-fill-fire-outcome-completed.v1"
    )


def test_golden_chain_the_operation_runs_through_its_binding(
    policy: ModelLabFillFireOutcomePolicy,
) -> None:
    (binding,) = _contract()["handler_routing"]["handlers"]
    assert binding["operation"] == "judge_lab_fill_fire"
    handler = getattr(
        importlib.import_module(binding["handler"]["module"]),
        binding["handler"]["name"],
    )()
    result = handler.handle(make_request(policy))
    assert isinstance(result, ModelLabFillFireOutcomeResult)
    assert (result.phase, result.exit, result.degraded_class, result.alert) == (
        "completed",
        0,
        None,
        None,
    )
    assert result.result_channel is EnumLabFillResultChannel.READ
    assert result.latest_value is not None
