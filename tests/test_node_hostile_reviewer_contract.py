# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Contract-routing validation tests for node_hostile_reviewer [OMN-9269].

Regression guard for the contract.yaml drift that degraded every hostile_reviewer
gate run to single-reviewer fallback mode. The prior contract declared 7 routing
handlers against only 1 subscribe topic and omitted event_model fields on every
entry, causing RuntimeLocal._validate_routing to reject the workflow.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from omnibase_core.runtime.runtime_local import RuntimeLocal

CONTRACT_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_hostile_reviewer"
    / "contract.yaml"
)


@pytest.fixture(scope="module")
def contract() -> dict[str, Any]:
    with CONTRACT_PATH.open() as fh:
        return yaml.safe_load(fh)


@pytest.mark.unit
class TestHostileReviewerContractRouting:
    """Ensure node_hostile_reviewer contract passes RuntimeLocal routing validation."""

    def test_routing_validation_passes(self, contract: dict[str, Any]) -> None:
        routing = contract.get("handler_routing", {})
        event_bus = contract.get("event_bus", {})
        errors = RuntimeLocal._validate_routing(
            routing,
            event_bus.get("subscribe_topics", []),
            event_bus.get("publish_topics", []),
        )
        assert errors == [], f"Routing validation failed: {errors}"

    def test_handlers_align_with_subscribe_topics(
        self, contract: dict[str, Any]
    ) -> None:
        handlers = contract["handler_routing"]["handlers"]
        subscribe_topics = contract["event_bus"]["subscribe_topics"]
        assert len(handlers) == len(subscribe_topics), (
            f"handlers ({len(handlers)}) must align positionally with "
            f"subscribe_topics ({len(subscribe_topics)})"
        )

    def test_every_handler_has_event_model(self, contract: dict[str, Any]) -> None:
        for idx, entry in enumerate(contract["handler_routing"]["handlers"]):
            em = entry.get("event_model", {})
            assert em.get("name"), f"handlers[{idx}].event_model.name missing"
            assert em.get("module"), f"handlers[{idx}].event_model.module missing"
