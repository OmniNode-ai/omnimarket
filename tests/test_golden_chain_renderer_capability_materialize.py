# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Live-dispatch-path materialization proof for node_renderer_capability_projection.

OMN-13131 (G-C). The W5 handler-isolation tests (the pure fold + the
``handle(envelope) -> ModelHandlerOutput`` path) already pass, yet the live
effects runtime logged, once per omnidash capability heartbeat::

    Projection handler error: handler=HandlerRendererCapabilityProjection
    topic=onex.cmd.ui.renderer-capability-declared.v1
    error_type=AttributeError error="'dict' object has no attribute 'payload'"

and the projection ``row_count`` stayed 0. Root cause: the contract declares
``db_io.db_tables`` + ``projection_api``, so the runtime wires this node through
the *projection* dispatch path
(``handler_wiring._make_projection_dispatch_callback``) — which delivers a
flattened domain payload **dict** + an injected ``_db`` adapter, calls
``handle(input_data)``, and **discards** any returned ``ModelHandlerOutput``. A
reducer-shaped ``handle(envelope)`` both crashed on ``dict.payload`` and would
never have written a row.

This module feeds the EXACT on-wire bytes the omnidash W-cap producer emits
(``server/renderer-capability-producer.ts``: ``{envelope_id,
envelope_timestamp, correlation_id, source_tool, payload:{capability,
declared_at}, transport}``) through the *real* runtime path:

    on-wire JSON bytes
      -> ModelEventEnvelope.model_validate           (the runtime's deserialize)
        -> MessageDispatchEngine materialize-to-dict  (the real materialization)
          -> _make_projection_dispatch_callback       (the real projection wiring)
            -> HandlerRendererCapabilityProjection.handle(input_data)
              -> UPSERT into renderer_capability_projection

and asserts a row MATERIALIZES (``row_count > 0``). A handler that took
``handle(envelope)`` fails this with the live AttributeError; a handler that
returned ``ModelHandlerOutput`` (dropped by the projection path) leaves
``row_count == 0``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from omnimarket.events.topics import RENDERER_CAPABILITY_DECLARED_TOPIC_V1
from omnimarket.nodes.node_renderer_capability_projection.handlers.handler_renderer_capability_projection import (
    TABLE,
)
from omnimarket.projection.protocol_database import InmemoryDatabaseAdapter

OMNIDASH_ANALYTICS_DB_URL_ENV = "OMNIDASH_ANALYTICS_DB_URL"


def _wcap_producer_wire_bytes(
    *,
    renderer_id: str = "ui.effect.web",
    declared_at: datetime,
) -> bytes:
    """The exact JSON the omnidash W-cap thin producer publishes to the bus.

    Mirrors ``buildCapabilityDeclarationEnvelope`` in
    ``omnidash/server/renderer-capability-producer.ts``: a bespoke outer object
    whose fields are valid ``ModelEventEnvelope`` fields (envelope_id,
    envelope_timestamp, correlation_id, source_tool, payload) plus an extra
    ``transport`` marker the canonical envelope ignores.
    """
    iso = declared_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
    envelope = {
        "envelope_id": "11111111-1111-4111-8111-111111111111",
        "envelope_timestamp": iso,
        "correlation_id": "22222222-2222-4222-8222-222222222222",
        "source_tool": "omnidash-ui",
        "payload": {
            "capability": {
                "renderer_id": renderer_id,
                "platform": "web",
                "supported_component_kinds": ["chart", "table"],
                "interaction_model": "pointer",
                "accessibility_tier": "aa",
                "contract_version": {"major": 1, "minor": 0, "patch": 0},
                "supports_interaction": True,
            },
            "declared_at": iso,
        },
        "transport": {
            "kind": "thin-publish",
            "producer": "omnidash-renderer-capability-producer",
            "topic": RENDERER_CAPABILITY_DECLARED_TOPIC_V1,
        },
    }
    return json.dumps(envelope).encode("utf-8")


def _materialized_dispatch_from_wire(raw: bytes) -> dict[str, object]:
    """Reproduce the runtime path: deserialize the wire bytes into the canonical
    ``ModelEventEnvelope`` then materialize it to the dict the dispatch engine
    hands a dispatcher callback.

    Uses the real ``ModelEventEnvelope.model_validate`` (the runtime's
    deserialize at ``handler_wiring._make_event_bus_callback``) and the real
    ``MessageDispatchEngine._materialize_envelope_with_bindings`` (the
    serialization boundary every dispatcher crosses), so the dict the projection
    callback receives is byte-identical to production.
    """
    from omnibase_core.models.events.model_event_envelope import ModelEventEnvelope
    from omnibase_infra.runtime.message_dispatch_engine import MessageDispatchEngine

    data = json.loads(raw.decode("utf-8"))
    envelope: ModelEventEnvelope[object] = ModelEventEnvelope[object].model_validate(
        data
    )
    engine = MessageDispatchEngine()
    return engine._materialize_envelope_with_bindings(
        envelope, {}, RENDERER_CAPABILITY_DECLARED_TOPIC_V1
    )


@pytest.mark.unit
class TestRendererCapabilityLiveDispatchMaterializes:
    """The real projection dispatch path materializes a row (no AttributeError)."""

    async def test_wcap_wire_bytes_materialize_through_real_projection_callback(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from omnibase_infra.runtime.auto_wiring import handler_wiring

        from omnimarket.nodes.node_renderer_capability_projection.handlers.handler_renderer_capability_projection import (
            HandlerRendererCapabilityProjection,
        )

        db = InmemoryDatabaseAdapter()
        # The projection callback builds a sync DB adapter from
        # OMNIDASH_ANALYTICS_DB_URL. Patch the builder to hand back the in-memory
        # adapter and set the env so the callback does not no-op on a missing DSN.
        monkeypatch.setenv(OMNIDASH_ANALYTICS_DB_URL_ENV, "postgresql://test/db")
        monkeypatch.setattr(handler_wiring, "_build_sync_db_adapter", lambda _dsn: db)

        callback = handler_wiring._make_projection_dispatch_callback(
            HandlerRendererCapabilityProjection(),
            db_tables=[{"name": TABLE, "database": "omnidash_analytics"}],
            subscribe_topics=(RENDERER_CAPABILITY_DECLARED_TOPIC_V1,),
        )

        # The live producer sets declared_at = now (fresh heartbeat), so the
        # observer-clock freshness derivation must classify it not-degraded.
        raw = _wcap_producer_wire_bytes(declared_at=datetime.now(tz=UTC))
        materialized = _materialized_dispatch_from_wire(raw)

        # Drive the REAL projection dispatch callback with the materialized dict.
        await callback(materialized)

        rows = db.query(TABLE)
        assert len(rows) == 1, (
            "live projection dispatch produced no row — projection did not "
            f"materialize (rows={rows})"
        )
        row = rows[0]
        assert row["renderer_id"] == "ui.effect.web"
        assert row["platform"] == "web"
        assert row["supported_component_kinds"] == ["chart", "table"]
        assert row["interaction_model"] == "pointer"
        assert row["accessibility_tier"] == "aa"
        assert row["contract_version"] == "1.0.0"
        assert row["is_degraded"] is False
        assert row["empty_state_reason"] is None

    async def test_reheartbeat_upserts_not_duplicates_through_real_path(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from omnibase_infra.runtime.auto_wiring import handler_wiring

        from omnimarket.nodes.node_renderer_capability_projection.handlers.handler_renderer_capability_projection import (
            HandlerRendererCapabilityProjection,
        )

        db = InmemoryDatabaseAdapter()
        monkeypatch.setenv(OMNIDASH_ANALYTICS_DB_URL_ENV, "postgresql://test/db")
        monkeypatch.setattr(handler_wiring, "_build_sync_db_adapter", lambda _dsn: db)

        callback = handler_wiring._make_projection_dispatch_callback(
            HandlerRendererCapabilityProjection(),
            db_tables=[{"name": TABLE, "database": "omnidash_analytics"}],
            subscribe_topics=(RENDERER_CAPABILITY_DECLARED_TOPIC_V1,),
        )

        t0 = datetime(2026, 6, 18, 12, 0, 0, tzinfo=UTC)
        await callback(
            _materialized_dispatch_from_wire(_wcap_producer_wire_bytes(declared_at=t0))
        )
        await callback(
            _materialized_dispatch_from_wire(
                _wcap_producer_wire_bytes(declared_at=t0 + timedelta(seconds=30))
            )
        )

        rows = db.query(TABLE)
        assert len(rows) == 1, "re-heartbeat must UPSERT, not duplicate"
        assert rows[0]["renderer_id"] == "ui.effect.web"
