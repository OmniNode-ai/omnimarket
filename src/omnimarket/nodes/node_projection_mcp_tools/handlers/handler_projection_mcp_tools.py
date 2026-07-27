"""HandlerProjectionMcpTools — project MCP tool registration events to the mcp_tools table.

Consumes onex.evt.platform.node-registration.v1 events published by node_contract_registry,
filters for mcp_eligible entries, and UPSERTs into the mcp_tools snapshot table.
The projection is exposed through the projection API for the omnidash NC-07
widget (palette-hidden).

Replay-safe and idempotent: re-processing the same registration event for a given
tool_name overwrites the existing row (UPSERT on tool_name).
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.nodes.node_projection_mcp_tools.models.enums import (
    EnumFreshnessState,
    EnumMcpToolStatus,
)
from omnimarket.nodes.node_projection_mcp_tools.models.model_mcp_tool_projection import (
    ModelMcpToolProjection,
)
from omnimarket.projection.handler_shim import split_projection_input
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "mcp_tools"
CONFLICT_KEY = "tool_name"

# Freshness thresholds from contract.yaml freshness_sla
MAX_LAG_SECONDS = 60
DEGRADED_AFTER_SECONDS = 120


class ModelMcpToolRegistrationEvent(BaseModel):
    """Inbound event from onex.evt.platform.node-registration.v1.

    Fields are a subset of ModelContractRegistrationResult as serialised to the bus.
    Extra fields are silently ignored so forward-compatible payloads are handled.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    node_name: str = Field(..., description="Name of the registered node / MCP tool.")
    contract_hash: str = Field(default="")
    correlation_id: str = Field(default="")
    status: str = Field(default="registered")
    mcp_eligible: bool = Field(
        default=False, description="True when the node exposes MCP tool capabilities."
    )
    mcp_tags: tuple[str, ...] = Field(
        default=(),
        description="MCP capability tags (e.g. 'mcp-enabled', 'mcp-tool:<name>').",
    )
    deployer_id: str = Field(default="")
    target_profile: str = Field(default="")
    emitted_at: str | None = Field(
        default=None, description="ISO 8601 event emission timestamp."
    )
    source_topic: str = Field(default="")
    source_partition: int = Field(default=0, ge=0)
    source_offset: int = Field(default=0, ge=0)
    event_id: str = Field(default="")
    # Supplementary contract metadata (description, model_id) forwarded via this field.
    contract_metadata: dict[str, object] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _derive_mcp_eligibility_from_tags(cls, data: object) -> object:
        """Derive mcp_eligible/mcp_tags from the real producer wire shape (OMN-14005).

        node_generation_consumer._emit_registration (OMN-13607 WS-C Phase 0.3)
        emits a generic ``tags: list[str]`` field (e.g. "mcp-enabled",
        "mcp-tool:<name>") — it deliberately does NOT send `mcp_eligible`/
        `mcp_tags` (that field pair was removed from the producer's contract as
        legacy, see test_registration_payload_tags_are_mcp_conformant in
        node_generation_consumer's tests). Without this, every real
        generation-sourced registration event silently acked with
        rows_upserted=0 — mcp_eligible defaulted False and no node ever became
        an invokable mcp_tools row. When the caller hasn't explicitly supplied
        mcp_eligible/mcp_tags, derive them here from the real `tags` list so a
        generation-sourced registration is recognised as eligible.
        """
        if not isinstance(data, dict):
            return data
        tags = data.get("tags")
        if not isinstance(tags, (list, tuple)):
            return data
        str_tags = tuple(str(t) for t in tags)
        derived = dict(data)
        if "mcp_eligible" not in derived:
            derived["mcp_eligible"] = "mcp-enabled" in str_tags
        if "mcp_tags" not in derived:
            derived["mcp_tags"] = str_tags
        return derived


class ModelMcpToolsProjectionAppliedEvent(BaseModel):
    """Outbound projection-mcp-tools-applied event payload."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str
    rows_upserted: int
    table: str = TABLE
    projection: dict[str, object]
    freshness_state: str
    applied_at: str


def _compute_freshness_state(
    emitted_at: str | None,
    now: datetime,
) -> EnumFreshnessState:
    """Compute freshness state based on event timestamp vs wall clock."""
    if emitted_at is None:
        return EnumFreshnessState.DEGRADED

    try:
        event_ts = datetime.fromisoformat(emitted_at.replace("Z", "+00:00"))
    except ValueError:
        return EnumFreshnessState.DEGRADED

    lag_seconds = (now - event_ts.astimezone(UTC)).total_seconds()

    if lag_seconds <= MAX_LAG_SECONDS:
        return EnumFreshnessState.FRESH
    if lag_seconds <= DEGRADED_AFTER_SECONDS:
        return EnumFreshnessState.STALE
    return EnumFreshnessState.DEGRADED


class HandlerProjectionMcpTools:
    """Project MCP-eligible node registration events into the mcp_tools table."""

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Expects a DatabaseAdapter at input_data['_db'].
        Non-MCP events (mcp_eligible=False) are acknowledged with rows_upserted=0.
        """
        db, payload, _meta = split_projection_input(input_data)
        event = ModelMcpToolRegistrationEvent(**payload)
        result = self.project(event, db)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelMcpToolRegistrationEvent,
        db: DatabaseAdapter,
    ) -> ModelMcpToolsProjectionAppliedEvent:
        """UPSERT an MCP tool registration event.

        Returns an applied event with rows_upserted=0 when the registration
        is not mcp_eligible — the event is acknowledged but nothing is written.
        """
        now = datetime.now(tz=UTC)
        now_iso = now.isoformat()

        if not event.mcp_eligible:
            return ModelMcpToolsProjectionAppliedEvent(
                tool_name=event.node_name,
                rows_upserted=0,
                projection={},
                freshness_state=EnumFreshnessState.FRESH,
                applied_at=now_iso,
            )

        # OMN-14532: this was `status = ACTIVE; if event.status == "rejected":
        # status = REJECTED` — permanently dead. Both real producers gate
        # eligibility before status matters: node_contract_registry's reject
        # path (_reject()) never sets mcp_eligible=True (model default is
        # False and it is never overridden there), and it publishes to a
        # DIFFERENT topic (node-registration-rejected.v1) this node does not
        # even subscribe to. node_generation_consumer never sends a `status`
        # field carrying "rejected" at all. Every event that reaches this
        # point (past the `mcp_eligible` gate above) is, by construction,
        # never a rejection — so status is always ACTIVE here. Removed the
        # unreachable branch rather than leave code that implies a capability
        # (rejecting a registration) this projector cannot actually exercise.
        status = EnumMcpToolStatus.ACTIVE

        # Extract supplementary fields from forwarded contract metadata.
        meta = event.contract_metadata
        description = str(meta.get("description", meta.get("tool_description", "")))
        model_id = str(meta.get("modelId", meta.get("model_id", "")))

        projection = ModelMcpToolProjection(
            tool_name=event.node_name,
            description=description,
            model_id=model_id,
            correlation_id=str(event.correlation_id),
            status=status,
            is_active=status == EnumMcpToolStatus.ACTIVE,
            mcp_tags=event.mcp_tags,
            metadata=meta,
            registered_at=event.emitted_at or now_iso,
            projected_at=now_iso,
        )

        freshness_state = _compute_freshness_state(event.emitted_at, now)
        row = projection.model_dump(mode="json")
        # mcp_tags stored as list for DB compatibility (JSONB / TEXT[]).
        row["mcp_tags"] = list(event.mcp_tags)
        db.upsert(TABLE, CONFLICT_KEY, row)

        return ModelMcpToolsProjectionAppliedEvent(
            tool_name=event.node_name,
            rows_upserted=1,
            projection=row,
            freshness_state=freshness_state,
            applied_at=now_iso,
        )

    def project_batch(
        self,
        events: list[ModelMcpToolRegistrationEvent],
        db: DatabaseAdapter,
    ) -> list[ModelMcpToolsProjectionAppliedEvent]:
        """UPSERT a batch of MCP tool registration events."""
        return [self.project(event, db) for event in events]


__all__: list[str] = [
    "HandlerProjectionMcpTools",
    "ModelMcpToolRegistrationEvent",
    "ModelMcpToolsProjectionAppliedEvent",
]
