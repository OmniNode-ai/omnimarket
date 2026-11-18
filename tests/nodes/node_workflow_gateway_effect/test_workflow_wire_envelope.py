# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""SEAM: the published wire payload against the REAL consumer models and subscribed topics.

Exemplar: tests/nodes/node_plan_list/test_golden_chain_plan_list.py (contract-declared chain). Either
side changing shape fails here: the gateway's wire builder, or the consumer node's input model or
subscribed command topic.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from uuid import UUID

import pytest
import yaml

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models import ModelDelegationRequest
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_wire_envelope import (
    to_canonical_wire_envelope,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_catalog import (
    ModelWorkflowCatalogEntry,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_envelope import (
    ModelWorkflowCommandEnvelope,
)
from tests.nodes.node_workflow_gateway_effect.fakes import (
    NOW,
    SKILL_PAYLOAD,
    TENANT_A,
    sample_catalog,
)

pytestmark = pytest.mark.unit
NODES = Path(__file__).resolve().parents[3] / "src/omnimarket/nodes"


def _entry(workflow_type: str) -> ModelWorkflowCatalogEntry:
    return next(
        e for e in sample_catalog().workflows if e.workflow_type == workflow_type
    )


def _envelope(
    entry: ModelWorkflowCatalogEntry,
    payload: dict[str, object],
    emitted_at: datetime = NOW,
) -> ModelWorkflowCommandEnvelope:
    return ModelWorkflowCommandEnvelope(
        envelope_id=UUID(int=1),
        correlation_id=UUID(int=2),
        causation_id=UUID(int=1),
        source_tenant_id=TENANT_A,
        authenticated_user_id="key-a",
        schema_version="1.1.0",
        emitted_at=emitted_at,
        source_gateway_instance="gateway-test",
        source_tenant_principal_id="t-" + TENANT_A.hex,
        workflow_id=UUID(int=3),
        workflow_type=entry.workflow_type,
        contract_id=entry.contract_id,
        command_topic=entry.command_topic,
        event_type=entry.event_type,
        payload=payload,
    )


def test_delegate_skill_wire_shape_and_consumer_model() -> None:
    entry = _entry("delegate-skill")
    wire = to_canonical_wire_envelope(
        _envelope(entry, {**SKILL_PAYLOAD, "max_tokens": 5}), "tenant-a", entry.wire
    )
    assert list(wire) == [
        "payload",
        "envelope_id",
        "envelope_timestamp",
        "correlation_id",
        "tenant_id",
        "source_tool",
        "metadata",
        "event_type",
        "priority",
        "retry_count",
        "onex_version",
        "envelope_version",
    ]
    assert list(wire["payload"]) == [
        "prompt",
        "task_type",
        "source",
        "max_tokens",
        "correlation_id",
        "tenant_id",
    ]
    # The canonical tenant UUID, never the slug, for a consumer that authorizes on it.
    assert wire["payload"]["tenant_id"] == str(TENANT_A)
    assert wire["tenant_id"] == str(TENANT_A)
    assert wire["metadata"]["tags"]["workflow_id"] == str(UUID(int=3))
    # SEAM: the real consumer model accepts exactly this payload.
    request = ModelDelegateSkillRequest.model_validate(wire["payload"])
    assert str(request.tenant_id) == str(TENANT_A)
    assert str(request.correlation_id) == str(UUID(int=2))


def test_delegation_inference_wire_stamps_and_consumer_model() -> None:
    entry = _entry("delegation-inference")
    # A non-UTC emitted_at must still render as UTC with a Z suffix.
    emitted = NOW.astimezone(timezone(timedelta(hours=5)))
    wire = to_canonical_wire_envelope(
        _envelope(
            entry,
            {"prompt": "p", "task_type": "test", "max_tokens": 3, "context_pack": "cp"},
            emitted,
        ),
        "tenant-a",
        entry.wire,
    )
    assert wire["envelope_timestamp"] == "2026-11-13T12:00:00Z"
    assert wire["payload"]["emitted_at"] == "2026-11-13T12:00:00Z"
    assert wire["payload"]["context_pack_hash"] == sha256(b"cp").hexdigest()
    assert wire["payload"]["tenant_id"] == str(TENANT_A)
    request = ModelDelegationRequest.model_validate(wire["payload"])
    assert request.context_pack == "cp"


def test_slug_and_principal_stamps_follow_the_spec() -> None:
    entry = _entry("push-validation")
    wire = to_canonical_wire_envelope(
        _envelope(entry, {"repo": "r", "ignored": 1}), "tenant-a", entry.wire
    )
    assert wire["payload"]["tenant_id"] == "tenant-a"  # attribution only
    assert wire["payload"]["tenant_principal_id"] == "t-" + TENANT_A.hex
    assert "ignored" not in wire["payload"]  # only the declared keys pass through
    no_slug = to_canonical_wire_envelope(
        _envelope(entry, {"repo": "r"}), None, entry.wire
    )
    assert "tenant_id" not in no_slug["payload"]


def test_wire_constants_are_copies_not_shared_state() -> None:
    entry = _entry("delegate-skill")
    first = to_canonical_wire_envelope(
        _envelope(entry, SKILL_PAYLOAD), None, entry.wire
    )
    first["onex_version"]["major"] = 99
    second = to_canonical_wire_envelope(
        _envelope(entry, SKILL_PAYLOAD), None, entry.wire
    )
    assert second["onex_version"] == {"major": 1, "minor": 0, "patch": 0}


@pytest.mark.parametrize(
    ("workflow_type", "consumer"),
    [
        ("delegate-skill", "node_delegate_skill_orchestrator"),
        ("delegation-inference", "node_delegation_orchestrator"),
    ],
)
def test_command_topic_is_one_the_consumer_node_subscribes_to(
    workflow_type: str, consumer: str
) -> None:
    contract = yaml.safe_load((NODES / consumer / "contract.yaml").read_text())
    subscribed = contract["event_bus"]["subscribe_topics"]
    topic = _entry(workflow_type).command_topic
    assert topic in subscribed, (
        f"{workflow_type}: {topic} is not subscribed by {consumer}"
    )
