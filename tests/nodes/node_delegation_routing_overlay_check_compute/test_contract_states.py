# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""node_delegation_routing_overlay_check_compute: declared outputs and topics are exercised (OMN-22976).

The state-coverage gate reads the node's contract outputs and publish topics and requires
the repo-level tests to reference each one. This file ties every declared output to a field
of the result the handler returns for the shipped documents, and every declared topic to the
topic the node's own contract names.
"""

from __future__ import annotations

import pytest

from omnimarket.nodes.node_delegation_routing_overlay_check_compute.handlers.handler_delegation_routing_overlay_check import (
    HandlerDelegationRoutingOverlayCheck,
)
from omnimarket.nodes.node_delegation_routing_overlay_check_compute.models.model_routing_overlay_check import (
    ModelRoutingOverlayCheckRequest,
)
from omnimarket.nodes.node_delegation_routing_overlay_check_compute.tests.support import (
    CONTRACT,
    class_names,
    contracts,
    load,
    load_topics,
    overlay,
)

pytestmark = pytest.mark.unit

CHECKED_TOPIC = "onex.evt.omnimarket.delegation-routing-overlay-checked.v1"
FAILED_TOPIC = "onex.evt.omnimarket.delegation-routing-overlay-check-failed.v1"


def _result():
    return HandlerDelegationRoutingOverlayCheck().handle(
        ModelRoutingOverlayCheckRequest(
            task_class_contracts=contracts(), bars_overlay=overlay()
        )
    )


def test_declared_outputs_are_the_result_fields() -> None:
    declared = load(CONTRACT)["outputs"]
    assert isinstance(declared, dict)
    assert set(declared) == {"ok", "checked_classes", "findings"}
    assert set(declared) == set(type(_result()).model_fields)


def test_shipped_documents_read_ok_with_every_class_checked() -> None:
    result = _result()
    assert result.ok is True
    assert result.checked_classes == len(class_names())
    assert result.findings == ()


def test_published_topics_are_the_contract_topics() -> None:
    topics = load_topics()
    assert topics["overlay_checked"] == CHECKED_TOPIC
    assert topics["overlay_check_failed"] == FAILED_TOPIC
    event_bus = load(CONTRACT)["event_bus"]
    assert isinstance(event_bus, dict)
    assert event_bus["publish_topics"] == [CHECKED_TOPIC, FAILED_TOPIC]
