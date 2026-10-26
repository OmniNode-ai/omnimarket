# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Invoke the packaged contract with a typed request (OMN-21373)."""

from __future__ import annotations

import importlib
from dataclasses import is_dataclass
from pathlib import Path
from typing import get_type_hints

import pytest
import yaml

pytestmark = pytest.mark.unit


def test_contract_golden_chain(tmp_path: Path) -> None:
    package = importlib.import_module(
        "omnimarket.nodes.node_lane_placement_recovery_effect"
    )
    assert package.__file__ is not None
    contract = yaml.safe_load(
        (Path(package.__file__).parent / "contract.yaml").read_text()
    )
    metadata = yaml.safe_load(
        (Path(package.__file__).parent / "metadata.yaml").read_text()
    )
    assert contract["name"] == "node_lane_placement_recovery_effect"
    assert contract["lifecycle"] == "experimental"
    assert "handler_routing" not in contract
    assert "transport_type" not in contract["metadata"]
    assert contract["metadata"]["related_tickets"] == ["OMN-21373"]
    assert (
        contract["runtime_profiles"] == contract["descriptor"]["runtime_profiles"] == []
    )
    assert metadata["pack"] == "distributed_validation"
    binding = contract["handler"]
    handler_class = getattr(
        importlib.import_module(binding["module"]), binding["class"]
    )

    def model(ref: str) -> type:
        module, _, name = ref.rpartition(".")
        result = getattr(importlib.import_module(module), name)
        assert is_dataclass(result)
        assert result.__dataclass_params__.frozen
        return result

    request_class = model(binding["input_model"])
    result_class = model(binding["output_model"])
    hints = get_type_hints(handler_class.handle)
    assert hints["request"] is request_class
    assert hints["return"] is result_class
    assert binding["class"] == "HandlerClaudeAccountLimit"
    assert "event_bus" not in contract
    failover_handler_class = importlib.import_module(
        "omnimarket.nodes.node_lane_placement_recovery_effect.handlers.handler_lane_engine_failover"
    ).HandlerLaneEngineFailover
    assert callable(failover_handler_class.handle)
    assert get_type_hints(failover_handler_class.handle)["request"] is model(
        "omnimarket.nodes.node_lane_placement_recovery_effect.models.model_lane_engine_failover.ModelLaneEngineFailoverRequest"
    )
    # The placement recovery operations are called in process through node.py.
    node_class = importlib.import_module(
        "omnimarket.nodes.node_lane_placement_recovery_effect.node"
    ).NodeLanePlacementRecoveryEffect
    for operation in (
        "placement_wait",
        "until_placed",
        "end_unplaced",
        "without_queued_no_host",
        "requeue_lost_placements",
        "placement_refusals",
    ):
        assert operation in contract["description"]
        assert callable(getattr(node_class, operation))
    result = handler_class({"load_pool": lambda: []}).handle(
        request_class(
            operation="revoke",
            host="host-a",
            text="no pool token",
        )
    )
    assert isinstance(result, result_class)
    assert result.has_room is False
    assert result.account is None
    from omnimarket.nodes.node_lane_placement_recovery_effect.handlers.handler_lane_engine_failover import (
        parse_route,
        rewrite_route,
    )

    brief = "# Task\nROUTE: model=opus effort=high\nDo the task.\n"
    rewritten, before, after = rewrite_route(brief, "codex")
    assert parse_route(brief)["model"] == "opus"
    assert parse_route(rewritten)["model"] == "codex"
    assert before == "ROUTE: model=opus effort=high"
    assert after == "ROUTE: model=codex effort=high"
