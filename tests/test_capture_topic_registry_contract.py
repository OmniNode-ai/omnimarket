# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""OMN-13151 Phase 0 contract tests for the durable-capture topics.

These tests load the canonical tiered registry (``registries/topics.yaml``) the
emit daemon loads at runtime and assert the OMN-13151 invariants for the two
durable-capture topics:

* both capture topics declare ``tier: duty_critical``;
* both declare a ``schema_ref``;
* both declare a ``max_payload`` that is bounded and strictly below the daemon
  stream cap (so the outbox never accepts a record the daemon would reject);
* a ratchet rejects any *new* duty-critical topic that is not on the explicit
  allowlist, forcing a deliberate decision (and a max_payload bound) for every
  duty-critical fan-out target.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnimarket.nodes.node_emit_daemon.event_registry import EventRegistry
from omnimarket.nodes.node_emit_daemon.models.model_durability import (
    EnumDurabilityTier,
)

# Default daemon stream cap (model_emit_daemon_config.max_payload_bytes default).
# Capture topic max_payload must stay strictly below this so a duty-critical
# record accepted by the registry can never be rejected by the daemon.
_DAEMON_PAYLOAD_CAP_BYTES = 1_048_576

_REGISTRY_PATH = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "omnimarket"
    / "nodes"
    / "node_emit_daemon"
    / "registries"
    / "topics.yaml"
)

_CAPTURE_EVENTS = ("artifact.captured", "tool.output.captured")

# Explicit allowlist of duty-critical event types. The ratchet fails if the
# loaded registry grows a duty-critical fan-out target not listed here, so a new
# duty-critical topic cannot land without a deliberate edit to this set (which
# is the moment to confirm tier + max_payload bounds).
_DUTY_CRITICAL_ALLOWLIST = frozenset(
    {
        "session.outcome",
        "compliance.evaluate",
        "pattern.enforcement",
        "delegation.request",
        "delegate.task",
        "intent.commit.bound",
        "change.frame.emitted",
        "gate.decision",
        "dod.verify.completed",
        "dod.guard.fired",
        "dod.sweep.completed",
        "audit.dispatch.validated",
        "audit.scope.violation",
        "artifact.captured",
        "tool.output.captured",
    }
)


@pytest.fixture(scope="module")
def registry() -> EventRegistry:
    return EventRegistry.from_yaml(_REGISTRY_PATH)


@pytest.mark.parametrize("event_type", _CAPTURE_EVENTS)
def test_capture_topic_duty_critical_tier(
    registry: EventRegistry, event_type: str
) -> None:
    """Both capture topics must be declared duty_critical at load time."""
    registration = registry.get_registration(event_type)
    assert registration is not None, f"{event_type} not in registry"
    assert registration.fan_out, f"{event_type} has no fan-out rules"
    for rule in registration.fan_out:
        assert rule.tier is EnumDurabilityTier.DUTY_CRITICAL, (
            f"{event_type} -> {rule.topic} declares tier {rule.tier}, "
            "expected duty_critical"
        )


@pytest.mark.parametrize("event_type", _CAPTURE_EVENTS)
def test_capture_topic_schema_ref_present(
    registry: EventRegistry, event_type: str
) -> None:
    """Both capture topics must declare a schema_ref."""
    registration = registry.get_registration(event_type)
    assert registration is not None
    for rule in registration.fan_out:
        assert rule.schema_ref, f"{event_type} -> {rule.topic} missing schema_ref"


@pytest.mark.parametrize("event_type", _CAPTURE_EVENTS)
def test_capture_topic_max_payload_bounded_below_daemon_cap(
    registry: EventRegistry, event_type: str
) -> None:
    """max_payload must be set, positive, and strictly below the daemon cap."""
    registration = registry.get_registration(event_type)
    assert registration is not None
    for rule in registration.fan_out:
        assert rule.max_payload_bytes is not None, (
            f"{event_type} -> {rule.topic} missing max_payload"
        )
        assert rule.max_payload_bytes > 0
        assert rule.max_payload_bytes < _DAEMON_PAYLOAD_CAP_BYTES, (
            f"{event_type} -> {rule.topic} max_payload "
            f"{rule.max_payload_bytes} must be < daemon cap "
            f"{_DAEMON_PAYLOAD_CAP_BYTES}"
        )


def test_no_unallowlisted_duty_critical_topic(registry: EventRegistry) -> None:
    """Ratchet: a new duty-critical event type must be added to the allowlist.

    This forces a deliberate decision (and a max_payload bound) for every
    duty-critical fan-out target rather than letting one drift in silently.
    """
    duty_critical_events = {
        event_type
        for event_type in registry.list_event_types()
        for rule in (registry.get_registration(event_type) or _NoFanout()).fan_out
        if rule.tier is EnumDurabilityTier.DUTY_CRITICAL
    }
    unexpected = duty_critical_events - _DUTY_CRITICAL_ALLOWLIST
    assert not unexpected, (
        "New duty-critical topics not on the allowlist: "
        f"{sorted(unexpected)}. Add each to _DUTY_CRITICAL_ALLOWLIST after "
        "confirming tier and (for capture topics) a max_payload bound."
    )


class _NoFanout:
    """Sentinel with an empty fan_out list for the ``or`` fallback above."""

    fan_out: list[object] = []
