# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The V2 waiter can hear a genuine canonical pre-dispatch failure."""

from __future__ import annotations

import asyncio
import errno
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.models.delegation.wire import ModelDelegationFailed
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.event_bus.models.model_event_message import ModelEventMessage
from omnibase_infra.runtime.contract_topic_router import (
    build_topic_router_from_contract,
)
from omnibase_infra.runtime.service_delegation_dispatch_port import (
    RuntimeDelegationDispatchPort as DeployedRuntimeDispatchPort,
)

from omnimarket.models.delegation.wire.model_delegate_skill_request import (
    ModelDelegateSkillRequest,
)
from omnimarket.models.delegation.wire.model_delegate_skill_response import (
    ModelDelegateSkillFailed,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.handlers.handler_delegate_skill import (
    HandlerDelegateSkill,
)
from omnimarket.nodes.node_delegate_skill_orchestrator.ports.port_runtime_delegation_dispatch import (
    RuntimeDelegationDispatchPort,
    load_runtime_delegation_dispatch_config,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers import (
    handler_delegation_workflow as workflow_module,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("port_kind", ["runtime", "market"])
async def test_real_handler_terminal_reaches_the_waiter_on_its_declared_topic(
    monkeypatch: pytest.MonkeyPatch,
    port_kind: str,
) -> None:
    config = load_runtime_delegation_dispatch_config().model_copy(
        update={"wait_timeout_seconds": 1}
    )
    contract = Path(workflow_module.__file__).resolve().parents[1] / "contract.yaml"
    router = build_topic_router_from_contract(yaml.safe_load(contract.read_text()))
    handler = workflow_module.HandlerDelegationWorkflow(workflows={})
    original = Path.read_text

    def unreadable(path: Path, *args: object, **kwargs: object) -> str:
        if path.name == "task_class_contracts.v1.yaml":
            raise OSError(errno.EMFILE, "Too many open files", str(path))
        return original(path, *args, **kwargs)

    bus = EventBusInmemory(environment="test", group="ingress-contract-failure")
    await bus.start()
    published: list[str] = []

    async def on_command(message: ModelEventMessage) -> None:
        request = (
            ModelEventEnvelope[ModelDelegationRequest]
            .model_validate_json(message.value)
            .payload
        )
        # Only the inner runtime is descriptor-exhausted in the observed
        # incident. The outer consumer must still resolve its execution budget.
        with monkeypatch.context() as fault:
            fault.setattr(Path, "read_text", unreadable)
            events = await handler.handle(request)
        for event in events:
            topic = router[type(event).__name__]
            published.append(topic)
            envelope = ModelEventEnvelope[dict[str, object]](
                payload=event.model_dump(mode="json"),
                correlation_id=request.correlation_id,
                envelope_timestamp=datetime.now(UTC),
                event_type=topic,
                source_tool="contract-failure-test",
            )
            await bus.publish(topic, None, envelope.model_dump_json().encode(), None)

    try:
        await bus.subscribe(
            config.topics.command, on_message=on_command, group_id="chain"
        )
        cid = uuid4()
        port = (
            DeployedRuntimeDispatchPort(event_bus=bus, package_names=("omnimarket",))
            if port_kind == "runtime"
            else RuntimeDelegationDispatchPort(event_bus=bus, config=config)
        )
        result = await asyncio.wait_for(
            port.dispatch(
                prompt="Write a PR body from facts.",
                task_type="document",
                correlation_id=cid,
                max_tokens=None,
                source_file_path=None,
                source_session_id=None,
                wait=True,
                execution_timeout_seconds=1,
                terminal_delivery_margin_seconds=1,
                quality_contract_mode="extend_task_class",
                acceptance_criteria=(),
                tenant_id="tenant-for-ingress-failure",
            ),
            timeout=10,
        )
        assert published == [config.topics.pre_dispatch_failed]
        assert result["status"] == "failed"
        assert result["correlation_id"] == str(cid)
        assert result["terminal_failure_reason"] == "delegation_contract_unavailable"
        assert result["attempts_count"] == 0
        assert result.get("quality_score") is None
        assert result["tenant_id"] == "tenant-for-ingress-failure"
        # Drive the outer consumer too: a canonical zero-attempt failure must
        # survive translation into the delegate-skill terminal the CLI awaits.
        terminal = await HandlerDelegateSkill(dispatch_port=port).handle(
            ModelDelegateSkillRequest(
                prompt="Write a PR body from facts.",
                task_type="document",
                source="claude-code",
                correlation_id=uuid4(),
                tenant_id="tenant-for-ingress-failure",
                requested_timeout_seconds=1,
            )
        )
        assert isinstance(terminal, ModelDelegateSkillFailed)
        assert terminal.attempts_count == 0
        assert terminal.terminal_failure_cause.value == "refused_before_dispatch"
        assert terminal.quality_score is None
        assert terminal.quality_gates_failed == []
    finally:
        await bus.close()


def _failure(cid: UUID, *, attempts: int = 0) -> ModelDelegationFailed:
    return ModelDelegationFailed(
        correlation_id=cid,
        task_type="document",
        model_used="none",
        endpoint_url="none",
        content="",
        quality_passed=False,
        operational_outcome="boundary_failure",
        content_verdict="not_applicable",
        latency_ms=0,
        attempts_count=attempts,
        fallback_to_claude=False,
        failure_reason="ingress contract could not be read",
        terminal_failure_reason="delegation_contract_unavailable",
    )


@pytest.mark.parametrize("case", ["routed", "malformed", "uncorrelated"])
async def test_pre_dispatch_subscription_does_not_accept_other_v1_events(
    case: str,
) -> None:
    config = load_runtime_delegation_dispatch_config()
    bus = EventBusInmemory(environment="test", group="pre-dispatch-filter")
    await bus.start()
    cid = uuid4()
    port = RuntimeDelegationDispatchPort(event_bus=bus, config=config)
    unsubscribe, queue = await port._subscribe_for_result(cid)
    try:
        assert config.topics.pre_dispatch_failed is not None
        terminal = _failure(
            uuid4() if case == "uncorrelated" else cid,
            attempts=1 if case == "routed" else 0,
        )
        payload = terminal.model_dump(mode="json")
        if case == "malformed":
            payload["quality_score"] = 1.0  # no model ran, so this cannot be scored
        envelope = ModelEventEnvelope[dict[str, object]](
            payload=payload,
            correlation_id=terminal.correlation_id,
            envelope_timestamp=datetime.now(UTC),
            event_type=config.topics.pre_dispatch_failed,
            source_tool="contract-failure-test",
        )
        await bus.publish(
            config.topics.pre_dispatch_failed,
            None,
            envelope.model_dump_json().encode(),
            None,
        )
        await asyncio.sleep(0)
        assert queue.empty()
        # Positive control: that same subscription accepts the valid own failure.
        own = envelope.model_copy(
            update={
                "payload": _failure(cid).model_dump(mode="json"),
                "correlation_id": cid,
            }
        )
        await bus.publish(
            config.topics.pre_dispatch_failed,
            None,
            own.model_dump_json().encode(),
            None,
        )
        result = await asyncio.wait_for(queue.get(), timeout=1)
        assert result.status == "failed"
        assert result.correlation_id == cid
    finally:
        await unsubscribe()
        await bus.close()


@pytest.mark.parametrize("cancelled", [False, True])
async def test_failed_optional_subscription_releases_prior_consumers(
    monkeypatch: pytest.MonkeyPatch, cancelled: bool
) -> None:
    config = load_runtime_delegation_dispatch_config()
    bus = EventBusInmemory(environment="test", group="partial-subscribe-cleanup")
    await bus.start()
    original = bus.subscribe
    closed: list[str] = []
    error = (
        asyncio.CancelledError() if cancelled else RuntimeError("subscription refused")
    )

    async def partial(topic: str, *args: object, **kwargs: object):
        if topic == config.topics.pre_dispatch_failed:
            raise error
        close = await original(topic, *args, **kwargs)

        async def unsubscribe() -> None:
            closed.append(topic)
            await close()

        return unsubscribe

    monkeypatch.setattr(bus, "subscribe", partial)
    try:
        with pytest.raises(type(error)):
            await RuntimeDelegationDispatchPort(
                event_bus=bus, config=config
            )._subscribe_for_result(uuid4())
        assert closed == [
            config.topics.completed,
            config.topics.failed,
            config.topics.failed_unrouted,
        ]
    finally:
        await bus.close()
