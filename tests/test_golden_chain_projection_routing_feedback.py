# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Rule-7a feedback fold and monotonic platform writer."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import pytest
import yaml
from omnibase_core.enums import EnumNodeKind
from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
from omnibase_infra.runtime.auto_wiring.handler_wiring import _make_dispatch_callback
from pydantic import ValidationError

from omnimarket.events.topics import (
    PROJECTION_ROUTING_FEEDBACK_APPLIED_TOPIC_V1,
    ROUTING_FEEDBACK_UPDATED_TOPIC_V1,
)
from omnimarket.models.delegation.model_routing_feedback import (
    ModelRoutingFeedback,
    ModelRoutingFeedbackUpdatedEvent,
)
from omnimarket.nodes.node_projection_routing_feedback.handlers import (
    HandlerProjectionRoutingFeedback,
    HandlerRoutingFeedbackWriter,
)
from omnimarket.nodes.node_projection_routing_feedback.models import (
    ModelRoutingFeedbackProjectionRequest,
)
from omnimarket.projection.runner import BaseProjectionRunner

if TYPE_CHECKING:
    from omnimarket.adapters.asyncpg_adapter import AsyncpgAdapter

pytestmark = pytest.mark.unit
NODE = (
    Path(__file__).resolve().parents[1]
    / "src/omnimarket/nodes/node_projection_routing_feedback"
)
T0 = datetime(2026, 10, 5, tzinfo=UTC)


def _event(
    *,
    count: int = 3,
    window: datetime = T0,
    model_id: str = "model-a",
    task_type: str = "codegen",
) -> dict[str, Any]:
    return ModelRoutingFeedbackUpdatedEvent(
        correlation_id="correlation-1",
        source_topic="",
        feedback=ModelRoutingFeedback(
            model_id=model_id,
            task_type=task_type,
            success_count=count - 1,
            failure_count=1,
            escalation_count=1,
            total_count=count,
            success_rate=(count - 1) / count,
            escalation_rate=1 / count,
            avg_latency_ms=120.0,
            window_start=window.isoformat(),
            last_updated=(window + timedelta(seconds=1)).isoformat(),
        ),
    ).model_dump(mode="json")


class _Store:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, Any]] = {}
        self.loop: asyncio.AbstractEventLoop | None = None

    async def connect(self) -> None:
        assert self.loop is None
        self.loop = asyncio.get_running_loop()

    async def close(self) -> None:
        self.loop = None

    async def execute(
        self, query: str, *params: Any, tenant: str | None = None
    ) -> list[dict[str, Any]]:
        assert self.loop is asyncio.get_running_loop()
        assert tenant is None
        assert "ON CONFLICT (model_id, task_type)" in query
        assert "RETURNING model_id" in query
        assert (
            "WHERE (public.delegation_routing_feedback.window_start, public.delegation_routing_feedback.total_count) "
            "< (EXCLUDED.window_start, EXCLUDED.total_count)"
        ) in " ".join(query.split())
        assert "window_start = EXCLUDED.window_start" in query
        columns = [
            c.strip() for c in query.split("(", 1)[1].split(")", 1)[0].split(",")
        ]
        row = dict(zip(columns, params, strict=True))
        assert isinstance(row["window_start"], datetime)
        assert isinstance(row["last_updated"], datetime)
        key = row["model_id"], row["task_type"]

        def order(value: dict[str, Any]) -> tuple[datetime, int]:
            return value["window_start"], value["total_count"]

        if key in self.rows and order(self.rows[key]) >= order(row):
            return []
        self.rows[key] = row
        return [{"model_id": row["model_id"]}]


def _writer() -> tuple[HandlerRoutingFeedbackWriter, _Store]:
    writer, store = HandlerRoutingFeedbackWriter(), _Store()
    writer._db = cast("AsyncpgAdapter", store)
    return writer, store


def test_fold_is_pure_and_order_independent() -> None:
    events = [
        _event(count=2),
        _event(count=4),
        _event(count=1, window=T0 + timedelta(days=1)),
    ]
    fold = HandlerProjectionRoutingFeedback()
    final = []
    for ordered in (events, list(reversed(events))):
        writer, store = _writer()
        for event in ordered:
            request = ModelRoutingFeedbackUpdatedEvent.model_validate(event)
            assert fold.handle(request) == fold.handle(request)
            assert (
                fold.handle(request).rows[0].model_dump(mode="json")
                == event["feedback"]
            )
            writer.handle(event)
        final.append(store.rows)
    assert final[0] == final[1]
    assert vars(fold) == {}


def test_newer_window_replaces_regardless_of_count() -> None:
    writer, store = _writer()
    writer.handle(_event(count=100))
    assert (
        writer.handle(_event(count=1, window=T0 + timedelta(seconds=1)))[
            "rows_upserted"
        ]
        == 1
    )
    assert store.rows[("model-a", "codegen")]["total_count"] == 1
    assert writer.handle(_event(count=200))["rows_refused_by_ordering_guard"] == 1


def test_lower_count_same_window_is_refused() -> None:
    writer, store = _writer()
    writer.handle(_event(count=4))
    before = dict(store.rows)
    for count in (2, 4):
        assert writer.handle(_event(count=count)) == {
            "rows_handled": 1,
            "rows_upserted": 0,
            "rows_refused_by_ordering_guard": 1,
            "feedback_rows": [{"model_id": "model-a", "task_type": "codegen"}],
        }
        assert store.rows == before
    assert writer.handle(_event(count=5))["rows_upserted"] == 1
    assert store.loop is None


def test_model_and_task_pairs_have_separate_rows() -> None:
    writer, store = _writer()
    for model, task in (
        ("model-a", "codegen"),
        ("model-b", "codegen"),
        ("model-a", "review"),
    ):
        writer.handle(_event(model_id=model, task_type=task))
    assert len(store.rows) == 3


def test_writer_declares_inprocess_dispatch() -> None:
    assert issubclass(HandlerRoutingFeedbackWriter, BaseProjectionRunner)
    assert HandlerRoutingFeedbackWriter.onex_runtime_inprocess_dispatch is True
    assert not getattr(
        HandlerProjectionRoutingFeedback, "onex_runtime_inprocess_dispatch", False
    )


def test_writer_subscribe_topics_equal_contract() -> None:
    contract = yaml.safe_load((NODE / "contract.yaml").read_text())
    assert contract["event_bus"]["subscribe_topics"] == [
        ROUTING_FEEDBACK_UPDATED_TOPIC_V1
    ]
    assert _writer()[0].subscribe_topics == contract["event_bus"]["subscribe_topics"]
    assert contract["terminal_event"] == PROJECTION_ROUTING_FEEDBACK_APPLIED_TOPIC_V1
    assert (
        PROJECTION_ROUTING_FEEDBACK_APPLIED_TOPIC_V1
        == "onex.evt.omnimarket.projection-routing-feedback-applied.v1"
    )
    assert contract["event_bus"]["publish_topics"] == [contract["terminal_event"]]
    (route,) = contract["handler_routing"]["handlers"]
    assert route["handler"]["name"] == "HandlerRoutingFeedbackWriter"
    assert contract["db_io"]["dedupe_key"] == ["model_id", "task_type"]
    assert contract["db_io"]["ordering_key"] == ["window_start", "total_count"]
    assert "projection_api" not in contract


def test_metadata_is_stripped_without_losing_correlation() -> None:
    writer, store = _writer()
    data = _event() | {
        "_db": object(),
        "_topic": ROUTING_FEEDBACK_UPDATED_TOPIC_V1,
        "_event_type": "routing-feedback-updated",
        "_partition": 2,
        "_offset": 7,
        "_envelope_timestamp": (T0 + timedelta(days=1)).isoformat(),
        "causation_id": "cause",
        "emitted_at": "later",
        "session_id": "session",
        "entity_id": "entity",
        "schema_version": "1.0",
    }
    request = ModelRoutingFeedbackProjectionRequest.model_validate(
        {
            key: value
            for key, value in data.items()
            if key not in {"_db", "_topic", "_partition", "_offset"}
        }
    )
    assert request.correlation_id == "correlation-1"
    assert writer.handle(data)["rows_handled"] == 1
    row = store.rows[("model-a", "codegen")]
    assert row["window_start"] == T0
    assert all(not key.startswith("_") for key in row)
    assert "_db" in data
    with pytest.raises(ValidationError):
        ModelRoutingFeedbackProjectionRequest.model_validate(
            _event() | {"unknown": "field"}
        )
    for model in (
        request,
        HandlerProjectionRoutingFeedback().handle(request),
        HandlerProjectionRoutingFeedback().handle(request).rows[0],
    ):
        with pytest.raises(ValidationError):
            setattr(model, next(iter(type(model).model_fields)), None)


async def test_runtime_reducer_kind_captures_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production wiring converts contract node_type into the node-kind enum."""
    monkeypatch.delenv("ONEX_CI_MODE", raising=False)
    callback = _make_dispatch_callback(
        HandlerProjectionRoutingFeedback(),
        None,
        handler_node_kind=EnumNodeKind.REDUCER,
        published_event_names=frozenset(),
    )
    result = await callback(ModelEventEnvelope[object](payload=_event()))
    assert result is not None
    assert not result.output_events
    assert len(result.projection_intents) == 1


def test_migration_declares_platform_table_and_existing_roles() -> None:
    ddl = (NODE / "migrations/0000_create_delegation_routing_feedback.sql").read_text()
    assert "PRIMARY KEY (model_id, task_type)" in ddl
    assert "window_start TIMESTAMPTZ NOT NULL" in ddl
    assert "last_updated TIMESTAMPTZ NOT NULL" in ddl
    assert "tenant_id" not in ddl
    assert "GRANT SELECT ON public.delegation_routing_feedback TO app_dashboard" in ddl
    assert "GRANT SELECT, INSERT, UPDATE" in ddl
    assert "TO omninode_runtime" in ddl
