# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation failure crosses the bus, is applied, and reads back as the same run failed (OMN-26122).

Each existing test stops at one handler or at an in-memory recorder. These carry one failure the
whole way: the delegation orchestrator's own failed terminal (a request it refuses, or a request it
accepted and routed whose inference leg then died at its consume boundary) or the dispatch engine's
``publisher_malformed`` refusal (produced by driving a real ``MessageDispatchEngine``) is
published on the in-memory bus, the REAL terminal writer (``HandlerGatewayWorkflowTerminal*`` over
``PostgresGatewayTerminalStore``) consumes it, and the REAL status reader
(``HandlerWorkflowStatusRead`` over ``PostgresWorkflowStore``) reads the same gateway row back from
a real Postgres. The run id the caller submitted is the run id the read returns, ``failed``, with
the typed class and code. The Postgres harness is the terminal writer's own
(``test_integration_real_postgres_terminal``): a throwaway database, skipped when none is reachable.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_DNS, UUID, uuid4, uuid5

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_core.models.dispatch.model_dispatch_route import ModelDispatchRoute
from omnibase_core.models.dispatch.model_handler_ref import ModelHandlerRef
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.enums import EnumDispatchStatus, EnumMessageCategory
from omnibase_infra.runtime.auto_wiring.handler_wiring import (
    PayloadTypeMatcher,
)
from omnibase_infra.runtime.boundary_failure_terminal import (
    ModelBoundaryFailureTerminal,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_spi.protocols.services import ProtocolSecretStore

from omnimarket.nodes.node_delegation_orchestrator.contract_topics import (
    TOPIC_ID_DELEGATION_FAILED,
)
from omnimarket.nodes.node_delegation_orchestrator.dispatchers.dispatcher_delegation_request import (
    DispatcherDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.handlers.handler_delegation_workflow import (
    INVALID_REQUEST_FAILURE_REASON,
    HandlerDelegationWorkflow,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_request import (
    ModelDelegationRequest,
)
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationFailed,
)
from omnimarket.nodes.node_delegation_routing_reducer.models.model_routing_decision import (
    ModelRoutingDecision,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers.handler_gateway_workflow_terminal import (
    HandlerGatewayWorkflowTerminal,
    HandlerGatewayWorkflowTerminalFailed,
    HandlerGatewayWorkflowTerminalRefusal,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.handlers.store_postgres import (
    PostgresGatewayTerminalStore,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_delegation_terminal_envelope import (
    ModelDelegationTerminalEnvelope,
)
from omnimarket.nodes.node_gateway_workflow_terminal_effect.models.model_gateway_terminal_applied import (
    EnumGatewayTerminalOutcome as Outcome,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.handler_workflow_status_read import (
    HandlerWorkflowStatusRead,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.store_postgres import (
    PostgresWorkflowStore,
)
from omnimarket.nodes.node_workflow_gateway_effect.handlers.workflow_postgres_pool import (
    WorkflowPostgresPool,
)
from omnimarket.nodes.node_workflow_gateway_effect.models.model_workflow_http import (
    ModelWorkflowCaller,
    ModelWorkflowStatusCommand,
)
from tests.nodes.node_gateway_workflow_terminal_effect.fakes import FakeDeadLetter
from tests.nodes.node_gateway_workflow_terminal_effect.test_integration_real_postgres_terminal import (  # noqa: F401  (the fixture is the exemplar's own)
    _FixedSecretStore,
    _insert,
    _pool,
    database,
)

pytestmark = pytest.mark.integration

_SKILL_CONTRACT = yaml.safe_load(
    (
        Path(__file__).resolve().parents[3]
        / "src"
        / "omnimarket"
        / "nodes"
        / "node_delegate_skill_orchestrator"
        / "contract.yaml"
    ).read_text("utf-8")
)
_SKILL_FAILED = _SKILL_CONTRACT["runtime_dispatch"]["terminal_events"]["failure"]
_SKILL_COMMAND = _SKILL_CONTRACT["runtime_dispatch"]["command_topic"]
_ENGINE_REFUSAL_CODE = "ONEX_CORE_345_ENVELOPE_VALIDATION_FAILED"
_LEG_FAILURE_CLASS = "ProtocolConfigurationError"
_LEG_FAILURE_CODE = "ONEX_CORE_041_INVALID_CONFIGURATION"
_INFERENCE_REQUEST_TOPIC = "onex.cmd.omnibase-infra.delegation-inference-request.v1"  # onex-topic-allow: the leg address the boundary stamps


def _caller(tenant: UUID) -> ModelWorkflowCaller:
    return ModelWorkflowCaller(
        tenant_id=tenant, principal_id="p", authenticated_user_id="u"
    )


def _status_reader(dsn: str) -> HandlerWorkflowStatusRead:
    pool = WorkflowPostgresPool(
        secret_resolver_config_path="unused",
        dsn_secret="unused",
        max_size=2,
        query_timeout_seconds=10.0,
        secret_store=cast(ProtocolSecretStore, _FixedSecretStore(dsn)),
    )
    store = PostgresWorkflowStore(
        pool=pool,
        metric_key="m",
        window_key="w",
        lock_namespace="n",
        rpm_metric_key="rm",
        rpm_window_key="rw",
        clock=lambda: datetime.now(UTC),
    )
    return HandlerWorkflowStatusRead(store=store)


async def _read(dsn: str, tenant: UUID, workflow_id: UUID) -> dict[str, Any]:
    response = await _status_reader(dsn).handle(
        ModelWorkflowStatusCommand(caller=_caller(tenant), workflow_id=str(workflow_id))
    )
    assert response.status_code == 200, response.body
    assert isinstance(response.body, dict)
    return response.body


def _writer_consumer(
    handler: HandlerGatewayWorkflowTerminal,
    topic: str,
    applied: list[Outcome],
) -> Callable[[Any], Awaitable[None]]:
    async def on_message(message: Any) -> None:
        result = await handler.handle(
            ModelDelegationTerminalEnvelope(
                topic=topic, payload=json.loads(message.value)
            )
        )
        applied.append(result.outcome)

    return on_message


async def _cross_the_bus(
    topic: str, payload: dict[str, Any], handler: HandlerGatewayWorkflowTerminal
) -> list[Outcome]:
    bus = EventBusInmemory()
    applied: list[Outcome] = []
    await bus.start()
    await bus.subscribe(
        topic, on_message=_writer_consumer(handler, topic, applied), group_id="writer"
    )
    await bus.publish(topic, key=None, value=json.dumps(payload).encode())
    await bus.close()
    return applied


async def _orchestrator_terminal(
    task_type: str, correlation_id: UUID, tenant: UUID
) -> dict[str, Any]:
    """The failed terminal the orchestrator really produces for a request it refuses."""
    result = await DispatcherDelegationRequest(
        HandlerDelegationWorkflow(workflows={})
    ).handle(
        {
            "payload": {
                "correlation_id": str(correlation_id),
                "task_type": task_type,
                "prompt": "say ok",
                "tenant_id": str(tenant),
                "emitted_at": datetime.now(UTC).isoformat(),
            },
            "__bindings": {},
            "__debug_trace": {"correlation_id": str(uuid4())},
        }
    )
    assert result.status == EnumDispatchStatus.SUCCESS
    terminals = [
        e for e in result.output_events if isinstance(e, ModelDelegationFailed)
    ]
    assert len(terminals) == 1
    return terminals[0].model_dump(mode="json")


async def test_a_refused_request_crosses_the_bus_and_reads_back_failed(
    database: str,  # noqa: F811
) -> None:
    tenant, correlation = uuid4(), uuid4()
    run_id = await _insert(database, tenant, correlation)
    assert (await _read(database, tenant, run_id))["status"] == "published"

    terminal = await _orchestrator_terminal(
        "omn19181_not_a_task_class", correlation, tenant
    )
    handler = HandlerGatewayWorkflowTerminalFailed(
        PostgresGatewayTerminalStore(_pool(database)), FakeDeadLetter()
    )
    applied = await _cross_the_bus(TOPIC_ID_DELEGATION_FAILED, terminal, handler)

    assert applied == [Outcome.APPLIED]
    body = await _read(database, tenant, run_id)
    assert body["workflow_id"] == str(run_id)
    assert body["correlation_id"] == str(correlation)
    assert body["status"] == "failed"
    assert body["terminal_failure_class"] == "DelegationRequestInvalidError"
    assert body["terminal_failure_code"] == "ONEX_DELEGATION_REQUEST_INVALID"
    assert INVALID_REQUEST_FAILURE_REASON.startswith(
        f"{body['terminal_failure_class']}: {body['terminal_failure_code']}"
    )
    assert body["terminal_model_used"] == "none"


async def _routed_workflow_terminal(
    correlation_id: UUID, tenant: UUID
) -> dict[str, Any]:
    """The failed terminal for a request the orchestrator accepted and routed, whose inference leg died.

    Not a refusal: the request is valid, a model is chosen, and the effect's consume boundary
    then gives up. Driven through the orchestrator's own ``handle()``, as the live dispatcher does.
    """
    handler = HandlerDelegationWorkflow(workflows={})
    await handler.handle(
        ModelDelegationRequest(
            prompt="Reply with the single word: alive.",
            task_type="research",  # type: ignore[arg-type]
            correlation_id=correlation_id,
            tenant_id=str(tenant),
            emitted_at=datetime.now(UTC),
        )
    )
    await handler.handle(
        ModelRoutingDecision(
            correlation_id=correlation_id,
            task_type="research",
            selected_model="local/routing-test-model",
            selected_backend_id=uuid5(
                NAMESPACE_DNS, "omninode.ai/backends/local-coder"
            ),
            endpoint_url="http://test-coder:8000",
            cost_tier="low",
            tier_name="free_local",
            max_context_tokens=65536,
            max_tokens=32,
            system_prompt="You are a test assistant.",
            rationale="Task 'research' routed via tier 'free_local'.",
        )
    )
    events = await handler.handle(
        ModelBoundaryFailureTerminal(
            correlation_id=correlation_id,
            failure_class=_LEG_FAILURE_CLASS,
            failure_code=_LEG_FAILURE_CODE,
            retryable=False,
            failure_reason=f"{_LEG_FAILURE_CLASS}: [{_LEG_FAILURE_CODE}] the leg gave up",
            origin_topic=_INFERENCE_REQUEST_TOPIC,
        )
    )
    terminals = [e for e in events if isinstance(e, ModelDelegationFailed)]
    assert len(terminals) == 1
    return terminals[0].model_dump(mode="json")


async def test_a_routed_request_whose_inference_leg_died_reads_back_failed(
    database: str,  # noqa: F811
) -> None:
    """A different failure from the refused request: accepted, routed to a model, then died mid-flight."""
    tenant, correlation = uuid4(), uuid4()
    run_id = await _insert(database, tenant, correlation)

    terminal = await _routed_workflow_terminal(correlation, tenant)
    assert terminal["model_used"] == "local/routing-test-model"
    handler = HandlerGatewayWorkflowTerminalFailed(
        PostgresGatewayTerminalStore(_pool(database)), FakeDeadLetter()
    )
    applied = await _cross_the_bus(TOPIC_ID_DELEGATION_FAILED, terminal, handler)

    assert applied == [Outcome.APPLIED]
    body = await _read(database, tenant, run_id)
    assert body["workflow_id"] == str(run_id)
    assert body["status"] == "failed"
    assert body["terminal_failure_class"] == _LEG_FAILURE_CLASS
    assert body["terminal_failure_code"] == _LEG_FAILURE_CODE
    assert body["terminal_failure_class"] != "DelegationRequestInvalidError"
    assert body["terminal_model_used"] == "local/routing-test-model"


async def _engine_refusal(correlation_id: UUID) -> dict[str, Any]:
    """The refusal a real dispatch engine gives a delegate-skill command whose payload fails its event model.

    A ``MessageDispatchEngine`` holds the delegate-skill dispatcher type-scoped to its declared
    ``event_model`` (``ModelDelegateSkillRequest``, whose ``task_type`` is a closed set). The command
    is dispatched on the contract's command topic; the engine classifies the miss and carries the
    class, code and validation detail on its result. The installed omnibase_infra stops at that
    result (the typed terminal model is not in it), so the wire shape the writer reads is mapped from
    the result's own fields here, and nothing in it is typed by hand.
    """
    handler_entry = next(
        h
        for h in _SKILL_CONTRACT["handler_routing"]["handlers"]
        if h["event_model"]["name"] == "ModelDelegateSkillRequest"
    )
    event_model = handler_entry["event_model"]
    engine = MessageDispatchEngine()
    engine.register_dispatcher(
        dispatcher_id="delegate_skill.request",
        dispatcher=_never_runs,
        category=EnumMessageCategory.COMMAND,
        message_types={event_model["name"]},
        payload_type_matcher=PayloadTypeMatcher(
            ModelHandlerRef(name=event_model["name"], module=event_model["module"])
        ),
    )
    engine.register_route(
        ModelDispatchRoute(
            route_id="delegate_skill.request.route",
            topic_pattern=_SKILL_COMMAND,
            message_category=EnumMessageCategory.COMMAND,
            handler_id="delegate_skill.request",
        )
    )
    engine.freeze()
    result = await engine.dispatch(
        topic=_SKILL_COMMAND,
        envelope=ModelEventEnvelope(
            payload={
                "correlation_id": str(correlation_id),
                "task_type": "omn19181_not_a_task_class",
                "prompt": "say ok",
            },
            correlation_id=correlation_id,
            event_type=event_model["name"],
        ),
    )
    assert result.status == EnumDispatchStatus.NO_DISPATCHER
    assert result.error_details["failure_class"] == "publisher_malformed"
    assert result.error_code is not None
    return {
        "correlation_id": str(correlation_id),
        "workflow_id": None,
        "status": "failed",
        "failure_class": result.error_details["failure_class"],
        "failure_code": result.error_code.value,
        "retryable": False,
        "failure_reason": result.error_details["validation_detail"],
        "origin_topic": result.topic,
    }


def _never_runs(envelope: object) -> None:
    raise AssertionError("the engine must refuse before the dispatcher runs")


async def test_a_publisher_malformed_refusal_crosses_the_bus_and_reads_back_failed(
    database: str,  # noqa: F811
) -> None:
    tenant, correlation = uuid4(), uuid4()
    run_id = await _insert(database, tenant, correlation)
    refusal = await _engine_refusal(correlation)
    assert refusal["failure_code"] == _ENGINE_REFUSAL_CODE
    handler = HandlerGatewayWorkflowTerminalRefusal(
        PostgresGatewayTerminalStore(_pool(database)), FakeDeadLetter()
    )
    applied = await _cross_the_bus(_SKILL_FAILED, refusal, handler)

    assert applied == [Outcome.APPLIED]
    body = await _read(database, tenant, run_id)
    assert body["workflow_id"] == str(run_id)
    assert body["status"] == "failed"
    assert body["terminal_failure_class"] == "PublisherMalformedError"
    assert body["terminal_failure_code"] == _ENGINE_REFUSAL_CODE


async def test_control_a_failure_for_another_run_leaves_this_run_published(
    database: str,  # noqa: F811
) -> None:
    """Known bad: the read says failed only for the run whose failure crossed the bus."""
    tenant, correlation = uuid4(), uuid4()
    run_id = await _insert(database, tenant, correlation)
    terminal = await _orchestrator_terminal(
        "omn19181_not_a_task_class", uuid4(), tenant
    )
    handler = HandlerGatewayWorkflowTerminalFailed(
        PostgresGatewayTerminalStore(_pool(database)), FakeDeadLetter()
    )
    applied = await _cross_the_bus(TOPIC_ID_DELEGATION_FAILED, terminal, handler)

    assert applied == [Outcome.NOT_A_GATEWAY_WORKFLOW]
    assert (await _read(database, tenant, run_id))["status"] == "published"
