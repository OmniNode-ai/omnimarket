# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_projection_read_effect (OMN-20159).

command envelope -> MessageDispatchEngine -> the production def-B adapter
(``_make_dispatch_callback``) -> ``HandlerProjectionRead.handle`` ->
``DispatchResultApplier`` -> the contract's terminal topic.

The seam the effects runtime wires from this contract, so it proves what the
unit suite cannot: the runtime validates the command payload into the
contract's request model, the handler's answer lands on the declared terminal
``onex.evt.omnimarket.projection-read-completed.v1``, and the tenant on a
consumed command envelope reaches the read (the dispatcher binds the envelope
around the handler call).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from omnibase_core.enums.enum_node_kind import EnumNodeKind
from omnibase_core.models.dispatch.model_dispatch_route import ModelDispatchRoute
from omnibase_core.models.dispatch.model_handler_ref import ModelHandlerRef
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.enums import EnumDispatchStatus, EnumMessageCategory
from omnibase_infra.protocols import ProtocolEventBusLike
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback
from omnibase_infra.runtime.event_bus_subcontract_wiring import (
    load_published_events_map,
)
from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine
from omnibase_infra.runtime.service_dispatch_result_applier import (
    DispatchResultApplier,
)

from omnimarket.nodes.node_projection_read_effect import (
    HandlerProjectionRead,
    ModelProjectionReadResult,
)
from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_read_effect/contract.yaml"
)
_COMMAND = "onex.cmd.omnimarket.projection-read.v1"
_TERMINAL = "onex.evt.omnimarket.projection-read-completed.v1"
_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"
_TENANT = "820272f9-4aaf-5add-a2df-0af942852ab2"


class _Source:
    backing = "table"

    def __init__(self) -> None:
        self.tenants: list[str | None] = []

    def unavailable(self, topic: str) -> tuple[str, str] | None:
        return None

    async def rows(self, cfg: Any, *, tenant_id: str | None, **_: Any) -> list[Any]:
        self.tenants.append(tenant_id)
        return [{"correlation_id": "c-1", "tenant_id": tenant_id, "written_at": None}]

    async def walk_origin(self, cfg: Any, **_: Any) -> None:
        return None

    async def latest_event_at(self, cfg: Any, **_: Any) -> None:
        return None

    def staleness(self, topic: str, latest_ts: str | None) -> dict[str, object]:
        return {"stale": False, "source": "table"}


def _topic_map() -> dict[str, ProjectionTableConfig]:
    columns = ("correlation_id", "tenant_id", "written_at")
    return {
        _DECISIONS: ProjectionTableConfig(
            topic=_DECISIONS,
            table="delegation_events",
            relation_schema="public",
            columns=columns,
            order_by="written_at DESC",
            order_by_spec=parse_order_by_clauses("written_at DESC", columns),
            limit=50,
            bus_backed=True,
            key_columns=("correlation_id",),
            tenant_column="tenant_id",
        )
    }


async def _run_chain(
    payload: dict[str, Any], *, envelope_tenant: str | None = None
) -> tuple[list[tuple[str, Any]], _Source]:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert _COMMAND in contract["event_bus"]["subscribe_topics"]
    assert contract["terminal_event"] == _TERMINAL
    entry = contract["handler_routing"]["handlers"][0]
    published_map = load_published_events_map(_CONTRACT)

    source = _Source()
    dispatcher = _make_dispatch_callback(
        HandlerProjectionRead(topic_map=_topic_map(), row_source=source),
        ModelHandlerRef(
            name=entry["event_model"]["name"], module=entry["event_model"]["module"]
        ),
        handler_node_kind=EnumNodeKind.EFFECT,
        published_event_names=frozenset(published_map),
    )
    engine = MessageDispatchEngine()
    engine.register_dispatcher(
        dispatcher_id="node_projection_read_effect.read",
        dispatcher=dispatcher,
        category=EnumMessageCategory.COMMAND,
        message_types={entry["event_model"]["name"]},
    )
    engine.register_route(
        ModelDispatchRoute(
            route_id="node_projection_read_effect.read.route",
            topic_pattern=_COMMAND,
            message_category=EnumMessageCategory.COMMAND,
            handler_id="node_projection_read_effect.read",
        )
    )
    engine.freeze()

    correlation_id = uuid4()
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=payload,
        correlation_id=correlation_id,
        event_type=entry["event_model"]["name"],
        tenant_id=envelope_tenant,
    )
    dispatch_result = await engine.dispatch(topic=_COMMAND, envelope=envelope)
    assert dispatch_result.status == EnumDispatchStatus.SUCCESS, (
        dispatch_result.error_message
    )

    published: list[tuple[str, Any]] = []

    class _Bus:
        async def publish_envelope(
            self, *, envelope: object, topic: str, key: bytes | None = None
        ) -> None:
            del key
            published.append((topic, envelope))

    applier = DispatchResultApplier(
        event_bus=cast("ProtocolEventBusLike", _Bus()),
        output_topic=contract["terminal_event"],
        output_topic_map=published_map,
        allowed_output_topics=contract["event_bus"]["publish_topics"],
    )
    await applier.apply(dispatch_result, correlation_id)
    return published, source


async def test_command_answers_a_page_on_the_terminal_topic() -> None:
    published, source = await _run_chain({"topic": _DECISIONS, "tenant_id": _TENANT})
    assert [topic for topic, _ in published] == [
        "onex.evt.omnimarket.projection-read-completed.v1"
    ]
    result = published[0][1].payload
    assert isinstance(result, ModelProjectionReadResult)
    dumped = result.model_dump()
    assert dumped["ok"] is True
    assert dumped["error"] is None
    assert dumped["tenant"] == _TENANT
    assert len(dumped["rows"]) == 1
    assert source.tenants == [_TENANT]


async def test_refusal_is_answered_on_the_terminal_topic_by_name() -> None:
    published, source = await _run_chain({"topic": _DECISIONS})
    assert [topic for topic, _ in published] == [_TERMINAL]
    dumped = published[0][1].payload.model_dump()
    assert dumped["ok"] is False
    assert dumped["error"] == "tenant_context_unresolved"
    assert dumped["rows"] == []
    assert source.tenants == []


async def test_consumed_envelope_tenant_reaches_the_read() -> None:
    published, source = await _run_chain({"topic": _DECISIONS}, envelope_tenant=_TENANT)
    dumped = published[0][1].payload.model_dump()
    assert dumped["ok"] is True, dumped
    assert dumped["tenant"] == _TENANT
    assert source.tenants == [_TENANT]


def test_runtime_can_construct_the_handler_without_arguments() -> None:
    """The runtime's resolver falls back to a zero-argument constructor.

    Construction must touch no database and read no contract: both are
    resolved on the first read.
    """
    handler = HandlerProjectionRead()
    assert handler._topic_map is None
    assert handler._owned_source is None
