# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Golden chain for node_projection_query_api_effect (OMN-21288, T17).

command envelope -> MessageDispatchEngine -> the production def-B adapter
(``_make_dispatch_callback``) -> ``HandlerProjectionQuery.handle`` ->
``DispatchResultApplier`` -> the contract's terminal topic.

The seam the effects runtime wires from this contract: the runtime validates the
command payload into the contract's request model, and the handler's answer to a
declared route lands on the declared terminal
``onex.evt.omnimarket.projection-query-completed.v1``.
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

from omnimarket.nodes.node_projection_query_api_effect import (
    HandlerProjectionQuery,
    ModelProjectionQueryResponse,
)
from omnimarket.projection.discovery import parse_order_by_clauses
from omnimarket.projection.models import ProjectionTableConfig
from omnimarket.projection.table_reader import ProtocolProjectionRowSource

pytestmark = pytest.mark.unit

_CONTRACT = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_query_api_effect/contract.yaml"
)
_COMMAND = "onex.cmd.omnimarket.projection-query.v1"
_TERMINAL = "onex.evt.omnimarket.projection-query-completed.v1"
_DECISIONS = "onex.snapshot.projection.delegation.decisions.v1"


class _Source:
    backing = "table"

    def health(self, topic_map: dict[str, ProjectionTableConfig]) -> dict[str, object]:
        return {"status": "ok", "served_topics": sorted(topic_map)}


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


async def _run_chain(payload: dict[str, Any]) -> list[tuple[str, Any]]:
    contract = yaml.safe_load(_CONTRACT.read_text(encoding="utf-8"))
    assert _COMMAND in contract["event_bus"]["subscribe_topics"]
    assert contract["terminal_event"] == _TERMINAL
    entry = contract["handler_routing"]["handlers"][0]
    published_map = load_published_events_map(_CONTRACT)

    dispatcher = _make_dispatch_callback(
        HandlerProjectionQuery(
            topic_map=_topic_map(),
            row_source=cast("ProtocolProjectionRowSource", _Source()),
        ),
        ModelHandlerRef(
            name=entry["event_model"]["name"], module=entry["event_model"]["module"]
        ),
        handler_node_kind=EnumNodeKind.EFFECT,
        published_event_names=frozenset(published_map),
    )
    engine = MessageDispatchEngine()
    engine.register_dispatcher(
        dispatcher_id="node_projection_query_api_effect.answer",
        dispatcher=dispatcher,
        category=EnumMessageCategory.COMMAND,
        message_types={entry["event_model"]["name"]},
    )
    engine.register_route(
        ModelDispatchRoute(
            route_id="node_projection_query_api_effect.answer.route",
            topic_pattern=_COMMAND,
            message_category=EnumMessageCategory.COMMAND,
            handler_id="node_projection_query_api_effect.answer",
        )
    )
    engine.freeze()

    correlation_id = uuid4()
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope(
        payload=payload,
        correlation_id=correlation_id,
        event_type=entry["event_model"]["name"],
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
    return published


async def test_command_answers_the_catalogue_on_the_terminal_topic() -> None:
    published = await _run_chain({"route_id": "projections"})
    assert [topic for topic, _ in published] == [_TERMINAL]
    result = published[0][1].payload
    assert isinstance(result, ModelProjectionQueryResponse)
    assert result.status_code == 200
    assert result.media_type == "application/json"
    assert [t["topic"] for t in result.body["topics"]] == [_DECISIONS]
    assert result.body["topics"][0]["tenant_scoped"] is True


async def test_command_answers_liveness_from_the_row_source() -> None:
    published = await _run_chain({"route_id": "health"})
    result = published[0][1].payload
    assert result.status_code == 200
    assert result.body == {"status": "ok", "served_topics": [_DECISIONS]}


def test_runtime_can_construct_the_handler_without_arguments() -> None:
    """Construction touches no database and reads no contract.

    The exposures and the row source are resolved on the first read.
    """
    handler = HandlerProjectionQuery()
    assert handler._topic_map is None
    assert handler._owned_source is None
