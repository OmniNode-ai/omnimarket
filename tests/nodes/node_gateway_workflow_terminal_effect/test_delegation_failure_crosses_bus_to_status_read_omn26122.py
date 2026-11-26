# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A delegation failure crosses the bus, is applied, and reads back as the same run failed (OMN-26122).

Each existing test stops at one handler or at an in-memory recorder. These carry one failure the
whole way: the delegation orchestrator's own failed terminal (a refused request, and the dying task
class the C16 and C28 probes submit) or the dispatch engine's ``publisher_malformed`` refusal is
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
from uuid import UUID, uuid4

import pytest
import yaml
from omnibase_core.event_bus.event_bus_inmemory import EventBusInmemory
from omnibase_infra.enums import EnumDispatchStatus
from omnibase_spi.protocols.services import ProtocolSecretStore

from omnimarket.nodes.node_delegation_check_probe_effect.handlers.gateway_checks import (
    DYING_TASK_TYPE,
)
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
from omnimarket.nodes.node_delegation_orchestrator.models.model_delegation_result import (
    ModelDelegationFailed,
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


@pytest.mark.parametrize("task_type", ["omn19181_not_a_task_class", DYING_TASK_TYPE])
async def test_a_refused_request_crosses_the_bus_and_reads_back_failed(
    database: str,  # noqa: F811
    task_type: str,
) -> None:
    tenant, correlation = uuid4(), uuid4()
    run_id = await _insert(database, tenant, correlation)
    assert (await _read(database, tenant, run_id))["status"] == "published"

    terminal = await _orchestrator_terminal(task_type, correlation, tenant)
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


async def test_a_publisher_malformed_refusal_crosses_the_bus_and_reads_back_failed(
    database: str,  # noqa: F811
) -> None:
    tenant, correlation = uuid4(), uuid4()
    run_id = await _insert(database, tenant, correlation)
    refusal = {
        "correlation_id": str(correlation),
        "workflow_id": None,
        "status": "failed",
        "failure_class": "publisher_malformed",
        "failure_code": _ENGINE_REFUSAL_CODE,
        "retryable": False,
        "failure_reason": "task_type: Input should be a declared task class",
        "origin_topic": _SKILL_COMMAND,
    }
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
