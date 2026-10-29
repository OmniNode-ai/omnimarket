# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_seam_classify_compute: operation_match routing of the registered seam operations (OMN-21459, OMN-21475)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from omnimarket.nodes.node_seam_classify_compute import NodeSeamClassifyCompute
from omnimarket.nodes.node_seam_classify_compute.handlers.handler_seam_classify import (
    HandlerSeamClassify,
)
from omnimarket.nodes.node_seam_classify_compute.models.model_seam_classify_request import (
    ModelSeamClassifyRequest,
    ModelSeamClassifyResult,
)

NODE = (
    Path(__file__).resolve().parents[3]
    / "src/omnimarket/nodes/node_seam_classify_compute"
)
REQUEST = ModelSeamClassifyRequest(
    operation="classify_sm_05", facts={}, now=datetime(2026, 10, 27, tzinfo=UTC)
)


def test_the_contract_routes_by_operation_to_the_registered_seam_operations() -> None:
    raw = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert raw["handler_routing"]["routing_strategy"] == "operation_match"
    assert [h["operation"] for h in raw["handler_routing"]["handlers"]] == [
        "classify_sm_00",
        "classify_sm_01",
        "classify_sm_02",
    ]
    assert "protocol_dependencies" not in raw


def test_an_unregistered_operation_refuses_naming_it() -> None:
    with pytest.raises(ValueError, match="classify_sm_05"):
        NodeSeamClassifyCompute().handle(REQUEST)


def test_a_registered_operation_is_dispatched() -> None:
    expected = ModelSeamClassifyResult(readings=())
    handler = HandlerSeamClassify({"classify_sm_05": lambda _request: expected})
    assert handler.handle(REQUEST) is expected


def test_the_contract_publishes_the_classified_event_on_its_terminal_topic() -> None:
    raw = yaml.safe_load((NODE / "contract.yaml").read_text(encoding="utf-8"))
    assert raw["terminal_event"] == "onex.evt.omnimarket.seam-classified.v1"
    assert raw["event_bus"]["publish_topics"] == [raw["terminal_event"]]
