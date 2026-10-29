# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-19996 Amendment 1: the deployed path names the key a run used.

Lab rehearsal 2026-10-29: two deployed-lane delegations on a tenant key ended
with a delegate-skill terminal whose secret_ref was null, because the bus
terminal carries none; the key's last_used_at never moved. The runtime port
now reads the key from the escalation_history rows the delegation workflow
stamps, and the writer moves every key a run named.

Failure modes, each with a test below:
D1 the port drops the key a customer attempt named.
D2 the port names a key other than the last one used.
D3 the port invents a key when no row names one.
D4 the deployed terminal still reaches the writer without the key.
D5 the writer moves only one of several keys a run used.
D6 a key the run used is moved without the never-lower guard.
D7 a ref that is not a minted tenant key, on an attempt, is written.
D8 a reader of escalation_history refuses a row that names a key.
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
SKILL_COMPLETED = "onex.evt.omnimarket.delegate-skill-completed.v1"


def _row(ref: str | None) -> dict[str, object]:
    """Build a single escalation-history row, optionally naming a secret ref."""
    row: dict[str, object] = {
        "tier_name": "tenant_byok",
        "model_used": "m-1",
        "backend_ref": "byok-openrouter",
        "acceptance_decision": "accept",
    }
    if ref is not None:
        row["secret_ref"] = ref
    return row


async def _dispatch_through_the_bus(
    history: tuple[dict[str, object], ...],
) -> dict[str, object]:
    """Run a real in-memory bus round-trip and return the terminal result dict."""
    bus = EventBusInmemory(environment="test", group="omn19996")
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
            source_tool="omn19996-test",
        )
        await bus.publish(
            TOPIC_COMPLETED, None, envelope.model_dump_json().encode(), None
        )

    try:
        await bus.subscribe(
            TOPIC_REQUEST, group_id=f"omn19996-{uuid4()}", on_message=on_command
        )
        port = RuntimeDelegationDispatchPort(
            event_bus=cast("Any", bus),
            completed_topic=TOPIC_COMPLETED,
            failed_topic=TOPIC_FAILED,
        )
        result = await port.dispatch(
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
    return result


@pytest.mark.asyncio
async def test_the_port_names_the_key_a_customer_attempt_used() -> None:
    """The port surfaces the secret_ref named by the customer's accepted attempt."""
    result = await _dispatch_through_the_bus((_row(X),))
    assert result["secret_ref"] == X


@pytest.mark.asyncio
async def test_the_port_takes_the_last_row_that_names_a_key() -> None:
    """The port picks the last escalation-history row that names a key."""
    result = await _dispatch_through_the_bus((_row(X), _row(None)))
    assert result["secret_ref"] == X

    result = await _dispatch_through_the_bus((_row(X), _row(Y)))
    assert result["secret_ref"] == Y


@pytest.mark.asyncio
async def test_the_port_invents_no_key() -> None:
    """The port omits secret_ref when no escalation row names one."""
    result = await _dispatch_through_the_bus((_row(None),))
    assert "secret_ref" not in result


@pytest.mark.asyncio
async def test_the_deployed_terminal_moves_last_used_at(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deployed terminal projection writes a last_used_at update for the named key."""

    class _Port:
        async def dispatch(self, **kwargs: Any) -> dict[str, object]:
            return await _dispatch_through_the_bus((_row(X),))

    handler = HandlerDelegateSkill(dispatch_port=_Port())
    response = await handler.handle(
        ModelDelegateSkillRequest(
            prompt="say hello",
            task_type="document",
            source="claude-code",
            correlation_id=uuid4(),
        )
    )
    assert response is not None
    assert response.secret_ref == X

    runner = HandlerTenantCredentialsProjectionRunner()
    db: Any = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    runner._db = db
    monkeypatch.setattr(runner, "publish_snapshot_delta", AsyncMock(return_value=True))

    data = response.model_dump(mode="json")
    data["_envelope_timestamp"] = "2026-10-29T19:47:17.643089+00:00"

    await runner.project_event(
        SKILL_COMPLETED,
        data,
        MessageMeta(partition=0, offset=1, fallback_id="fb", topic=SKILL_COMPLETED),
    )

    writes = [
        c
        for c in db.execute.await_args_list
        if "tenant_inference_credentials" in str(c.args[0])
    ]
    assert len(writes) == 1
    assert X in writes[0].args


def _runner(
    monkeypatch: pytest.MonkeyPatch,
) -> HandlerTenantCredentialsProjectionRunner:
    runner = HandlerTenantCredentialsProjectionRunner()
    db: Any = AsyncMock()
    db.execute = AsyncMock(return_value=[])
    runner._db = db
    monkeypatch.setattr(runner, "publish_snapshot_delta", AsyncMock(return_value=True))
    return runner


def _credential_writes(runner: HandlerTenantCredentialsProjectionRunner) -> list[Any]:
    db: Any = runner._db
    return [
        call
        for call in db.execute.await_args_list
        if "tenant_inference_credentials" in str(call.args[0])
    ]


def _terminal(top: str | None, attempt_refs: list[str | None]) -> dict[str, object]:
    data: dict[str, object] = {
        "correlation_id": str(uuid4()),
        "tenant_id": "tenant-a",
        "_envelope_timestamp": "2026-10-29T19:51:10.748183+00:00",
        "attempts": [
            {"tier": "tenant_byok", **({"secret_ref": ref} if ref else {})}
            for ref in attempt_refs
        ],
    }
    if top is not None:
        data["secret_ref"] = top
    return data


@pytest.mark.asyncio
async def test_every_key_a_run_used_moves(monkeypatch: pytest.MonkeyPatch) -> None:
    """A run that used X and then Y moves both rows, each once."""
    runner = _runner(monkeypatch)
    await runner.project_event(
        SKILL_COMPLETED,
        _terminal(Y, [X, Y]),
        MessageMeta(partition=0, offset=2, fallback_id="fb", topic=SKILL_COMPLETED),
    )
    refs = sorted(str(call.args[1]) for call in _credential_writes(runner))
    assert refs == [X, Y]


@pytest.mark.asyncio
async def test_each_key_keeps_the_never_lower_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every per-key UPDATE keeps the guard that refuses an older time."""
    runner = _runner(monkeypatch)
    await runner.project_event(
        SKILL_COMPLETED,
        _terminal(None, [X, Y]),
        MessageMeta(partition=0, offset=3, fallback_id="fb", topic=SKILL_COMPLETED),
    )
    writes = _credential_writes(runner)
    assert len(writes) == 2
    for call in writes:
        assert "last_used_at IS NULL OR last_used_at <" in str(call.args[0])
        assert "INSERT" not in str(call.args[0])


@pytest.mark.asyncio
async def test_a_house_ref_on_an_attempt_is_not_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ref without the minted tenant-key shape on an attempt writes nothing."""
    runner = _runner(monkeypatch)
    await runner.project_event(
        SKILL_COMPLETED,
        _terminal(None, ["llm.glm.api_key"]),
        MessageMeta(partition=0, offset=4, fallback_id="fb", topic=SKILL_COMPLETED),
    )
    assert _credential_writes(runner) == []


def test_every_reader_of_the_history_decodes_a_row_naming_a_key() -> None:
    """Each in-repo reader of escalation_history accepts a row carrying secret_ref.

    Searched 2026-10-29 at the merge base: omnibase_infra, omniclaude and
    omnidash read no escalation_history at all; inside omnimarket the readers
    are the delegate-skill handler's attempt ladder, the runtime port and the
    delegation projection, each exercised here on the same row.
    """
    from omnimarket.nodes.node_delegate_skill_orchestrator.handlers import (
        handler_delegate_skill as skill,
    )
    from omnimarket.nodes.node_projection_delegation.handlers import (
        handler_projection_delegation as projection,
    )

    history: tuple[dict[str, object], ...] = (_row(X),)
    result = ModelDelegationResult(
        correlation_id=uuid4(),
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
    )
    dumped = result.model_dump(mode="json")
    assert dumped["escalation_history"][0]["secret_ref"] == X
    records = skill._attempt_records(
        {"correlation_id": str(uuid4()), "escalation_history": list(history)}
    )
    assert [record.backend_id for record in records] == ["byok-openrouter"]
    assert isinstance(projection._winning_metered_tier_name(list(history)), str)
    projection._deciding_rung_stop_reason(list(history))
