# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live transport leg: absence of the lab broker is UNMEASURED and fails this proof."""

import os

import pytest
from omnibase_infra.event_bus.event_bus_kafka import EventBusKafka

from tests.nodes.test_rule_premise import runtime_rule_premise_roundtrip

pytestmark = pytest.mark.kafka


async def test_rule_premise_redpanda_declared_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bootstrap = os.environ.get("RULE_PREMISE_REDPANDA_BOOTSTRAP")
    assert bootstrap, (
        "UNMEASURED: RULE_PREMISE_REDPANDA_BOOTSTRAP must name the isolated lab broker"
    )
    await runtime_rule_premise_roundtrip(
        monkeypatch, EventBusKafka.from_bootstrap(bootstrap), "redpanda"
    )
