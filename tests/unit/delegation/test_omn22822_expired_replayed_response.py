# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A replayed inference response past the run's completion bound is expired, not re-routed (OMN-22822).

Both inputs were recorded on the dev lane:

* the workflow row ``c490e90f`` (fixture shared with OMN-22478): submitted
  2026-11-04T20:14Z, ``ROUTED`` on the tenant_overlay rung with inference
  attempt ``649b9b71`` in flight, recorded 2026-11-06T16:32Z;
* the response for that same attempt, re-delivered onto the live
  inference-response topic by the dead-letter replay node at
  2026-11-07T02:40:20Z (offset 108354, ``x-replay-count=1``), carrying the
  original 2026-11-04 failure "Failed to resolve secret from Infisical".

The run's start epoch is about 54 hours before that delivery and the contract
declares a 900 s completion bound. Before this change the orchestrator took the
replay as a live answer: the failure went to the retry and escalation ladder,
which on a run with retries left opens a new provider attempt. These tests fail
on that and pass when the replay is recorded as expired and the run closes
FAILED with no routing or inference intent.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from omnibase_core.enums.enum_delegation_operational_outcome import (
    EnumDelegationOperationalOutcome,
)
from omnibase_core.models.delegation.wire.model_delegation_failed import (
    ModelDelegationFailed,
)
from pydantic import BaseModel

from omnimarket.nodes.node_delegation_orchestrator.enums import EnumDelegationState
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    DelegationWorkflowState,
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_intent import (
    ModelInferenceIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_inference_response_data import (
    ModelInferenceResponseData,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_quality_gate_intent import (
    ModelQualityGateIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_routing_intent import (
    ModelRoutingIntent,
)
from omnimarket.nodes.node_delegation_orchestrator.state_codec import decode

pytestmark = pytest.mark.usefixtures("stub_provider_quota_reader")

_FIXTURES = Path(__file__).parent / "fixtures"
_ROW = _FIXTURES / "omn22478_abandoned_rows" / "c490e90f_superseded_rejections.json"
_REPLAY = _FIXTURES / "omn22822_replayed_responses" / "c490e90f_replayed_response.json"
_ATTEMPT = UUID("649b9b71-3ceb-5b11-8a2d-89c22c6c9e4a")
_NEW_ATTEMPT_TYPES = (ModelRoutingIntent, ModelInferenceIntent, ModelQualityGateIntent)


def _load(path: Path) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(path.read_text())
    return loaded


def _recorded_workflow() -> DelegationWorkflowState:
    workflow = decode(json.dumps(_load(_ROW)["payload"]))
    # The OMN-22478 row is scrubbed of its credential reference; the
    # customer-route retry budget is reset so the run still has a retry the
    # replay could open, as the 1,880 runs moved on 2026-11-07 did.
    workflow.customer_route_retry_count = 0
    return workflow


def _recorded_replay() -> ModelInferenceResponseData:
    record = _load(_REPLAY)
    assert record["headers"]["x-replayed-by"] == "node_dlq_replay_effect"
    return ModelInferenceResponseData.model_validate(record["payload"])


def _handle(
    workflow: DelegationWorkflowState,
) -> tuple[HandlerDelegationWorkflow, list[BaseModel]]:
    handler = HandlerDelegationWorkflow(workflows={workflow.correlation_id: workflow})
    return handler, handler.handle_inference_response(_recorded_replay())


@pytest.mark.unit
def test_recorded_pair_is_the_live_attempt_of_a_run_past_its_bound() -> None:
    workflow = _recorded_workflow()
    replay = _recorded_replay()
    assert workflow.state == EnumDelegationState.ROUTED
    assert workflow.current_inference_attempt_id == _ATTEMPT
    assert replay.inference_attempt_id == _ATTEMPT
    assert replay.error_message
    replayed_at_ns = _load(_REPLAY)["timestamp_ms"] * 1_000_000
    assert (replayed_at_ns - workflow.started_at_ns) // 1_000_000_000 > 900


@pytest.mark.unit
def test_replayed_response_past_bound_is_expired_and_not_rerouted() -> None:
    workflow = _recorded_workflow()
    _, events = _handle(workflow)

    assert not [e for e in events if isinstance(e, _NEW_ATTEMPT_TYPES)]
    expired = workflow.expired_inference_response
    assert expired is not None
    assert expired.attempt_id == _ATTEMPT
    assert expired.response_was_error is True
    assert expired.completion_bound_seconds == 900
    assert expired.elapsed_seconds > 900
    assert workflow.state == EnumDelegationState.FAILED
    terminals = [e for e in events if isinstance(e, ModelDelegationFailed)]
    assert len(terminals) == 1
    assert terminals[0].operational_outcome == EnumDelegationOperationalOutcome.TIMEOUT
    assert "recorded as expired and not re-routed" in terminals[0].failure_reason


@pytest.mark.unit
def test_a_second_delivery_of_the_replay_changes_nothing() -> None:
    workflow = _recorded_workflow()
    handler, _ = _handle(workflow)
    assert handler.handle_inference_response(_recorded_replay()) == []
    assert workflow.state == EnumDelegationState.FAILED


@pytest.mark.unit
def test_the_same_response_inside_the_bound_is_still_acted_on() -> None:
    workflow = _recorded_workflow()
    workflow.started_at_ns = time.time_ns()
    _, events = _handle(workflow)

    assert workflow.expired_inference_response is None
    assert [e for e in events if isinstance(e, _NEW_ATTEMPT_TYPES)] or [
        e for e in events if isinstance(e, ModelDelegationFailed)
    ]
