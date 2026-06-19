# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Renderer Capability Registry projection handler (OMN-13131 / W5).

Sole-writer projection for the Renderer Capability Registry. A renderer
thin-publishes a ``ModelRendererCapabilityDeclaration`` heartbeat onto the
renderer-capability-declared command topic (constant
``RENDERER_CAPABILITY_DECLARED_TOPIC_V1`` in ``omnimarket.events.topics``); this
handler folds each declaration into the heartbeat-backed projection and UPSERTs
one row per ``renderer_id`` into ``renderer_capability_projection``.

Runtime materialization protocol
--------------------------------
The contract declares ``db_io.db_tables`` + ``projection_api``, so the effects
runtime wires this node through the canonical *projection* dispatch path
(``omnibase_infra.runtime.auto_wiring.handler_wiring._make_projection_dispatch_callback``)
— the SAME path every materializing projection on the lane uses
(``HandlerProjectionSavings``, ``HandlerPrMergedProjection``, …). That path
delivers a *flattened domain payload dict* plus an injected ``DatabaseAdapter``
under ``input_data['_db']`` and the topic-derived ``input_data['_event_type']``,
then calls ``handle(input_data)`` and persists through the adapter. It does NOT
construct a ``ModelEventEnvelope`` and it discards any returned
``ModelHandlerOutput``. A handler that takes ``handle(envelope)`` and returns
``ModelHandlerOutput.for_reducer`` would (1) crash on ``dict.payload`` and (2)
never write a row even if it didn't — the projection is the only read authority,
so a dropped projection means ``row_count`` stays 0 and the registry never
materializes. Hence ``handle`` implements the projection-runner protocol.

The deterministic core stays pure: ``renderer_capability_fold.fold_declaration``
is the sole writer of capability rows and re-derives heartbeat-TTL freshness for
every row. This handler reads the prior projection rows back through the adapter,
folds the new declaration onto them, and UPSERTs the resulting rows — keyed on
``renderer_id`` so a re-heartbeat upserts (not duplicates) the row, and so a
stale renderer flips to ``is_degraded`` carrying
``EnumEmptyStateReason.UPSTREAM_BLOCKED`` rather than rendering blind.
"""

from __future__ import annotations

from datetime import UTC, datetime

from omnibase_core.enums.enum_accessibility_tier import EnumAccessibilityTier
from omnibase_core.enums.enum_empty_state_reason import EnumEmptyStateReason
from omnibase_core.enums.enum_renderer_interaction_model import (
    EnumRendererInteractionModel,
)
from omnibase_core.enums.enum_widget_type import EnumWidgetType
from omnibase_core.models.primitives.model_semver import ModelSemVer
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_renderer_capability_projection.models.model_renderer_capability_declaration import (
    ModelRendererCapabilityDeclaration,
)
from omnimarket.nodes.node_renderer_capability_projection.models.model_renderer_capability_projection_state import (
    DEFAULT_HEARTBEAT_TTL_SECONDS,
    ModelRendererCapabilityProjectionRow,
    ModelRendererCapabilityProjectionState,
)
from omnimarket.nodes.node_renderer_capability_projection.renderer_capability_fold import (
    fold_declaration,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

HANDLER_ID = "renderer-capability-projection-reducer"

# Projection table + UPSERT key. Mirrors the contract's db_io.db_tables entry and
# the unique constraint in 0001_create_renderer_capability_projection.sql.
TABLE = "renderer_capability_projection"
CONFLICT_KEY = "renderer_id"

# Runtime-injected keys the projection dispatch path adds alongside the domain
# payload. Stripped before the payload is validated into a declaration.
_DB_KEY = "_db"
_EVENT_TYPE_KEY = "_event_type"


class ModelProjectionResult(BaseModel):
    """Outcome of folding one capability heartbeat into the projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)


class HandlerRendererCapabilityProjection:
    """Sole-writer projection: fold one capability heartbeat and UPSERT its rows."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """Runtime projection-dispatch entrypoint (OMN-13131 / W5).

        The projection host delivers the flattened declaration payload plus a
        ``DatabaseAdapter`` under ``input_data['_db']``. Coerce the payload into a
        ``ModelRendererCapabilityDeclaration``, fold it onto the rows already in
        the table, and UPSERT the resulting rows. Fail fast when the adapter is
        absent — a missing adapter is a wiring bug, not recoverable state.
        """
        payload = dict(input_data)
        db_raw = payload.pop(_DB_KEY, None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        payload.pop(_EVENT_TYPE_KEY, None)
        declaration = ModelRendererCapabilityDeclaration.model_validate(payload)
        result = self.project(declaration, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        declaration: ModelRendererCapabilityDeclaration,
        db: DatabaseAdapter,
        *,
        observed_at: datetime | None = None,
        ttl_seconds: int = DEFAULT_HEARTBEAT_TTL_SECONDS,
    ) -> ModelProjectionResult:
        """Fold one heartbeat onto the persisted rows and UPSERT the result.

        Reads the prior projection rows back through the adapter so the pure fold
        can upsert the declaring renderer (keyed on ``renderer_id``) and
        re-derive TTL freshness for every other row. Every row in the folded
        state is UPSERTed so a TTL-lapsed sibling persists its
        ``is_degraded``/``empty_state_reason`` flip on the same write.
        """
        clock = observed_at or datetime.now(tz=UTC)
        prior_state = _load_prior_state(db)
        new_state = fold_declaration(
            prior_state,
            declaration,
            observed_at=clock,
            ttl_seconds=ttl_seconds,
        )
        upserted = 0
        for row in new_state.rows:
            if db.upsert(TABLE, CONFLICT_KEY, _row_to_columns(row, observed_at=clock)):
                upserted += 1
        return ModelProjectionResult(rows_upserted=upserted)


def _load_prior_state(db: DatabaseAdapter) -> ModelRendererCapabilityProjectionState:
    """Reconstruct the prior projection state from the persisted rows.

    The fold accumulates across heartbeats; the durable accumulator is the
    projection table itself (the sole read authority), so prior rows are read
    back rather than carried in process memory.
    """
    persisted = db.query(TABLE)
    rows = tuple(_columns_to_row(record) for record in persisted)
    return ModelRendererCapabilityProjectionState(rows=rows)


def _row_to_columns(
    row: ModelRendererCapabilityProjectionRow,
    *,
    observed_at: datetime,
) -> dict[str, object]:
    """Map a projection row to its table columns (JSON-safe scalars)."""
    return {
        "renderer_id": row.renderer_id,
        "platform": row.platform,
        "supported_component_kinds": [k.value for k in row.supported_component_kinds],
        "interaction_model": row.interaction_model.value,
        "accessibility_tier": row.accessibility_tier.value,
        "contract_version": str(row.contract_version),
        "declared_at": row.declared_at.astimezone(UTC).isoformat(),
        "last_heartbeat": row.last_heartbeat.astimezone(UTC).isoformat(),
        "is_degraded": row.is_degraded,
        "empty_state_reason": (
            row.empty_state_reason.value if row.empty_state_reason is not None else None
        ),
        "observed_at": observed_at.astimezone(UTC).isoformat(),
        "updated_at": observed_at.astimezone(UTC).isoformat(),
    }


def _columns_to_row(record: dict[str, object]) -> ModelRendererCapabilityProjectionRow:
    """Rebuild a projection row from a persisted table record."""
    return ModelRendererCapabilityProjectionRow(
        renderer_id=_as_str(record["renderer_id"]),
        platform=_as_str(record["platform"]),
        supported_component_kinds=tuple(
            EnumWidgetType(value)
            for value in _as_str_sequence(record["supported_component_kinds"])
        ),
        interaction_model=EnumRendererInteractionModel(
            _as_str(record["interaction_model"])
        ),
        accessibility_tier=EnumAccessibilityTier(_as_str(record["accessibility_tier"])),
        contract_version=ModelSemVer.parse(_as_str(record["contract_version"])),
        declared_at=_as_datetime(record["declared_at"]),
        last_heartbeat=_as_datetime(record["last_heartbeat"]),
        is_degraded=bool(record["is_degraded"]),
        empty_state_reason=(
            EnumEmptyStateReason(_as_str(record["empty_state_reason"]))
            if record.get("empty_state_reason") is not None
            else None
        ),
    )


def _as_str(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError(f"expected str column, got {type(value).__name__}")
    return value


def _as_str_sequence(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise TypeError(f"expected list column, got {type(value).__name__}")
    return tuple(_as_str(item) for item in value)


def _as_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    raise TypeError(f"expected datetime/str column, got {type(value).__name__}")


__all__: list[str] = [
    "CONFLICT_KEY",
    "HANDLER_ID",
    "TABLE",
    "HandlerRendererCapabilityProjection",
    "ModelProjectionResult",
]
