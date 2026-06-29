"""OMN-13363: the e2e-probe envelope must produce a delegation_events INSERT.

The 2026-06-19 Gate-Zero run reported that the 4 synthetic probe events were
consumed but wrote 0 rows ("probe-shaped delegation events write zero rows while
real traffic projects"). This test drives the EXACT probe wire shape — a
``ModelEventEnvelope`` whose ``payload`` is ``_build_delegation_payload`` —
through the real unwrap + dispatch path (``unwrap_envelope`` ->
``DelegationProjectionRunner.project_event`` -> ``db.execute``) with a recording
DB adapter, and asserts that:

1. the envelope unwraps to the probe payload (not a no-op / empty dict),
2. ``project_event`` dispatches to the task-delegated branch,
3. exactly one ``delegation_events`` INSERT is executed carrying the probe's
   identity fields (``correlation_id``, ``task_type=e2e_probe_harness``,
   ``delegated_to=claude-haiku-4-5``, ``quality_gate_passed=True``).

So the probe envelope is proven NOT to be a silent no-op at the code level; a
zero-row outcome on a live lane is a lane/broker-routing fact, not a handler bug.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope

from omnimarket.nodes.node_projection_delegation.handlers.handler_delegation import (
    DelegationProjectionRunner,
)
from omnimarket.projection.envelope import unwrap_envelope
from omnimarket.projection.runner import MessageMeta

# Probe identity — must stay byte-identical to
# tests/integration/e2e_probe/test_delegation_e2e_probe.py::_build_delegation_payload
_PROBE_TASK_TYPE = "e2e_probe_harness"
_PROBE_DELEGATED_TO = "claude-haiku-4-5"
_PROBE_DELEGATED_BY = "omnimarket_e2e_probe_harness"
_PROBE_SOURCE_TOOL = "omnimarket.e2e-probe-harness.omn-12789"


def _build_probe_payload(correlation_id: str) -> dict[str, Any]:
    return {
        "correlation_id": correlation_id,
        "task_type": _PROBE_TASK_TYPE,
        "delegated_to": _PROBE_DELEGATED_TO,
        "delegated_by": _PROBE_DELEGATED_BY,
        "quality_gate_passed": True,
        "timestamp": datetime.now(UTC).isoformat(),
        "cost_usd": 0.0,
        "cost_savings_usd": 0.001,
        "pricing_manifest_version": 1,
    }


def _build_probe_envelope_bytes(correlation_id: str) -> bytes:
    """Reproduce the probe's thin-publish wire bytes exactly."""
    payload = _build_probe_payload(correlation_id)
    envelope = ModelEventEnvelope[dict[str, Any]](
        payload=payload,
        correlation_id=uuid.UUID(correlation_id),
        source_tool=_PROBE_SOURCE_TOOL,
        event_type="omniclaude.task-delegated",
    )
    return json.dumps(envelope.model_dump(mode="json")).encode("utf-8")


@pytest.mark.asyncio
async def test_probe_envelope_unwraps_to_payload() -> None:
    """The probe envelope unwraps to the task-delegated payload, not an empty dict."""
    correlation_id = str(uuid.uuid4())
    unwrapped = unwrap_envelope(_build_probe_envelope_bytes(correlation_id))

    assert unwrapped is not None, "probe envelope must unwrap (not a non-event)"
    assert unwrapped["correlation_id"] == correlation_id
    assert unwrapped["task_type"] == _PROBE_TASK_TYPE
    assert unwrapped["delegated_to"] == _PROBE_DELEGATED_TO
    assert unwrapped["quality_gate_passed"] is True


@pytest.mark.asyncio
async def test_probe_envelope_upserts_delegation_row() -> None:
    """End-to-end (unwrap -> project_event -> execute): one INSERT with probe identity."""
    db = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    db.connect = AsyncMock()
    db.close = AsyncMock()

    runner = DelegationProjectionRunner(publish_fn=None)
    runner._db = db

    correlation_id = str(uuid.uuid4())
    unwrapped = unwrap_envelope(_build_probe_envelope_bytes(correlation_id))
    assert unwrapped is not None

    topic = runner._topic_delegated
    assert topic == "onex.evt.omniclaude.task-delegated.v1"

    meta = MessageMeta(partition=0, offset=0, fallback_id="probe-fallback")
    projected = await runner.project_event(topic, dict(unwrapped), meta)

    assert projected is True, "probe event must project (not be dropped as a no-op)"
    db.execute.assert_called_once()

    sql, *params = db.execute.call_args[0]
    assert "INSERT INTO delegation_events" in sql, (
        f"probe event must write delegation_events, got SQL: {sql[:80]}"
    )
    # Probe identity fields must be bound into the INSERT params.
    assert correlation_id in params
    assert _PROBE_TASK_TYPE in params
    assert _PROBE_DELEGATED_TO in params
    assert True in params  # quality_gate_passed
