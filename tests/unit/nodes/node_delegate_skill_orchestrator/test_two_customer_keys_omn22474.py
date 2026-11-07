# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-22474 AC2: a run that used two customer keys moves both last-used rows.

OMN-19996 PR 1 (omnimarket#4526) set the delegate-skill terminal's top-level
``secret_ref`` from the last escalation row that names one, and taught the
tenant-credentials writer to move every key a terminal names. The per-attempt
key stayed undeclared on the attempt record, so a run that used customer key X
and then Y moved only Y. This drives the deployed path end to end: a real
in-memory bus round-trip, the delegate-skill handler's terminal, and the writer.

Failure modes, each with a test below:
K1 only the last key a run used moves; the earlier customer key stays unstamped.
K2 a house-key attempt moves a row it does not own.
"""

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports import (
    RuntimeDelegationDispatchPort,
)
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_COMPLETED as TOPIC_COMPLETED,
)
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_FAILED as TOPIC_FAILED,
)
from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_REQUEST as TOPIC_REQUEST,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationResult,
)
from omnimarket.nodes.node_projection_tenant_credentials.handlers.handler_tenant_credentials_projection import (
    HandlerTenantCredentialsProjectionRunner,
)
from omnimarket.projection.runner import MessageMeta

pytestmark = pytest.mark.unit

X = "cred_tenant-a_openrouter_" + "a" * 32
Y = "cred_tenant-a_openrouter_" + "b" * 32
HOUSE = "OPENROUTER_API_KEY"
SKILL_COMPLETED = "onex.evt.omnimarket.delegate-skill-completed.v1"


def _row(ref: str, decision: str) -> dict[str, object]:
    return {
        "tier_name": "tenant_byok",
        "model_used": "m-1",
        "backend_ref": "byok-openrouter",
        "acceptance_decision": decision,
        "secret_ref": ref,
    }


async def _bus_result(history: tuple[dict[str, object], ...]) -> dict[str, object]:
    bus = EventBusInmemory(environment="test", group="omn22474")
    await bus.start()

    async def on_command(message: ModelEventMessage) -> None:
        request = (
            ModelEventEnvelope[ModelDelegationRequest]
            .model_validate_json(message.value)
            .payload
        )
        envelope = ModelEventEnvelope[ModelDelegationResult](
            payload=ModelDelegationResult(
                correlation_id=request.correlation_id,
                task_type="document",
                model_used="m-1",
                endpoint_url="https://openrouter.invalid/api/v1/chat/completions",
                content="hello",
                quality_passed=True,
                quality_score=1.0,
                latency_ms=10,
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=2,
                fallback_to_claude=False,
                failure_reason="",
                escalation_history=history,
                tenant_id="tenant-a",
            ),
            correlation_id=request.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=TOPIC_COMPLETED,
            source_tool="omn22474-test",
        )
        await bus.publish(
            TOPIC_COMPLETED, None, envelope.model_dump_json().encode(), None
        )

    try:
        await bus.subscribe(
            TOPIC_REQUEST, group_id=f"omn22474-{uuid4()}", on_message=on_command
        )
        port = RuntimeDelegationDispatchPort(
            event_bus=cast("Any", bus),
            completed_topic=TOPIC_COMPLETED,
            failed_topic=TOPIC_FAILED,
        )
        return await port.dispatch(
            prompt="say hello",
            task_type="document",
            correlation_id=uuid4(),
            max_tokens=64,
            source_file_path=None,
            source_session_id=None,
            wait=True,
            execution_timeout_seconds=60,
            terminal_delivery_margin_seconds=30,
            quality_contract_mode="replace_task_class",
            acceptance_criteria=(),
            tenant_id="tenant-a",
        )
    finally:
        await bus.close()


async def _written_refs(
    history: tuple[dict[str, object], ...], monkeypatch: pytest.MonkeyPatch
) -> list[str]:
    """The key refs the writer moves for one deployed-path run with this history."""

    class _Port:
        async def dispatch(self, **kwargs: Any) -> dict[str, object]:
            result = await _bus_result(history)
            # A run with one rung per history row counted that many calls.
            result["attempts_count"] = len(history)
            return result

    response = await HandlerDelegateSkill(dispatch_port=_Port()).handle(
        ModelDelegateSkillRequest(
            prompt="say hello",
            task_type="document",
            source="claude-code",
            correlation_id=uuid4(),
        )
    )
    assert response is not None

    runner = HandlerTenantCredentialsProjectionRunner()
    db: Any = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    runner._db = db
    monkeypatch.setattr(runner, "publish_snapshot_delta", AsyncMock(return_value=True))
    data = response.model_dump(mode="json")
    data["_envelope_timestamp"] = "2026-11-05T02:00:00+00:00"
    await runner.project_event(
        SKILL_COMPLETED,
        data,
        MessageMeta(partition=0, offset=1, fallback_id="fb", topic=SKILL_COMPLETED),
    )
    refs: list[str] = []
    for call in db.execute.await_args_list:
        if "tenant_inference_credentials" in str(call.args[0]):
            refs.extend(a for a in call.args[1:] if a in (X, Y, HOUSE))
    return refs


@pytest.mark.asyncio
async def test_two_customer_keys_both_rows_move(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs = await _written_refs((_row(X, "climb"), _row(Y, "accept")), monkeypatch)
    assert sorted(refs) == sorted([X, Y])


@pytest.mark.asyncio
async def test_two_customer_keys_house_attempt_moves_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    refs = await _written_refs((_row(X, "climb"), _row(HOUSE, "accept")), monkeypatch)
    assert refs == [X]
