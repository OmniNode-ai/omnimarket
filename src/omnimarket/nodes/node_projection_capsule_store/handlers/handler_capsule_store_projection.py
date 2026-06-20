# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""HandlerCapsuleStoreProjection -- fold ROI score events into capsule_store.

REDUCER projection node (OMN-12842 / M2). Consumes
``onex.evt.omnimarket.context-roi-score-completed.v1`` and materialises a
durable, scored capsule row keyed by the deterministic ``capsule_hash``:

  * effectiveness is ALWAYS populated from the ROI score event (never empty);
  * a changed exemplar (different content / commit / artifact / schema_version)
    is a NEW row, never an in-place mutation;
  * replay is idempotent -- the same scored event folds into one row with
    ``hit_count`` incremented deterministically;
  * staleness decay parameters are CONTRACT-declared and resolved here at the
    handler boundary -- no hardcoded constants and no env vars. Raw scored
    values stay immutable in the base table; ``effective_score`` is computed at
    read time.

Topic strings live ONLY in ``contract.yaml`` -- the handler resolves them from
the contract via :mod:`omnimarket.events.topics` constants.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.events.topics import (
    CAPSULE_STORE_APPLIED_TOPIC_V1,
    CONTEXT_ROI_SCORE_COMPLETED_TOPIC_V1,
)
from omnimarket.nodes.node_projection_capsule_store.models.model_capsule_identity import (
    EnumCapsuleSchemaVersion,
    ModelCapsuleIdentity,
)
from omnimarket.nodes.node_projection_capsule_store.models.model_capsule_record import (
    ModelCapsuleEffectiveness,
    ModelCapsuleRecord,
)
from omnimarket.projection.protocol_database import DatabaseAdapter

TABLE = "capsule_store"
# Natural identity key -- a changed exemplar yields a new capsule_hash and thus
# a distinct row; replay of the same exemplar folds into the existing row.
CONFLICT_KEY = "capsule_hash"

_SECONDS_PER_DAY = 86_400.0


class ModelCapsuleDecayConfig(BaseModel):
    """Contract-declared staleness-decay parameters.

    Resolved from ``contract.yaml`` ``config.decay`` at the handler boundary;
    never hardcoded constants and never env vars.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    half_life_days: float = Field(
        gt=0.0,
        description="Half-life of effectiveness decay in days.",
    )
    floor: float = Field(
        ge=0.0,
        le=1.0,
        description="Lower bound on the decay multiplier.",
    )


class ModelCapsuleScoredEvent(BaseModel):
    """Projection view of ``context-roi-score-completed.v1`` for one capsule.

    Carries the capsule provenance (identity fields) plus the ROI effectiveness
    numbers the scorer computed. ``final_success_rate`` maps to ``success_rate``
    and ``cost_per_success_usd`` to ``cost_per_success`` in the stored row.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    factor: EnumContextFactor = Field(description="Context factor category.")
    content: str = Field(min_length=1, description="Capsule content body.")
    source_artifact: str = Field(
        min_length=1, description="Source artifact path/reference."
    )
    source_commit: str = Field(
        min_length=1, description="Source commit the capsule was captured from."
    )
    schema_version: EnumCapsuleSchemaVersion = Field(
        description="Schema version of the capsule record."
    )
    validity_scope: str = Field(
        min_length=1,
        description="Scope the capsule is valid for (e.g. 'repo:omnimarket').",
    )
    final_success_rate: float = Field(
        ge=0.0, le=1.0, description="Maps to stored success_rate."
    )
    first_pass_rate: float = Field(
        ge=0.0, le=1.0, description="Maps to stored first_pass_rate."
    )
    cost_per_success_usd: float = Field(
        ge=0.0, description="Maps to stored cost_per_success."
    )
    event_timestamp: datetime = Field(
        description="Score event timestamp; stored as last_scored (tz-aware UTC)."
    )

    @field_validator("event_timestamp")
    @classmethod
    def validate_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("event_timestamp must be timezone-aware")
        return value.astimezone(UTC)

    def identity(self) -> ModelCapsuleIdentity:
        return ModelCapsuleIdentity.from_provenance(
            factor=self.factor,
            content=self.content,
            source_artifact=self.source_artifact,
            source_commit=self.source_commit,
            schema_version=self.schema_version,
        )

    def effectiveness(self, *, hit_count: int) -> ModelCapsuleEffectiveness:
        return ModelCapsuleEffectiveness(
            success_rate=self.final_success_rate,
            first_pass_rate=self.first_pass_rate,
            cost_per_success=self.cost_per_success_usd,
            hit_count=hit_count,
            last_scored=self.event_timestamp,
        )


class ModelProjectionResult(BaseModel):
    """Result of a capsule projection batch."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows_upserted: int = Field(default=0, ge=0)
    table: str = Field(default=TABLE)
    applied_topic: str = Field(default=CAPSULE_STORE_APPLIED_TOPIC_V1)


class HandlerCapsuleStoreProjection:
    """Fold ROI score events into the durable capsule_store projection."""

    def __init__(self, contract_path: Path | None = None) -> None:
        path = contract_path or Path(__file__).parent.parent / "contract.yaml"
        with open(path) as handle:
            contract: dict[str, Any] = yaml.safe_load(handle)

        event_bus = contract.get("event_bus", {})
        subscribe_topics = list(event_bus.get("subscribe_topics", []))
        publish_topics = list(event_bus.get("publish_topics", []))
        # Cross-check the contract against the canonical topic constants so a
        # drift between contract.yaml and the topic registry fails fast.
        if CONTEXT_ROI_SCORE_COMPLETED_TOPIC_V1 not in subscribe_topics:
            raise ValueError(
                "contract.yaml must subscribe to "
                f"{CONTEXT_ROI_SCORE_COMPLETED_TOPIC_V1}"
            )
        if CAPSULE_STORE_APPLIED_TOPIC_V1 not in publish_topics:
            raise ValueError(
                f"contract.yaml must publish {CAPSULE_STORE_APPLIED_TOPIC_V1}"
            )
        self._subscribe_topic = CONTEXT_ROI_SCORE_COMPLETED_TOPIC_V1
        self._applied_topic = CAPSULE_STORE_APPLIED_TOPIC_V1

        decay_raw = contract["config"]["decay"]
        self._decay_config = ModelCapsuleDecayConfig(
            half_life_days=float(decay_raw["half_life_days"]),
            floor=float(decay_raw["floor"]),
        )

    @property
    def subscribe_topic(self) -> str:
        return self._subscribe_topic

    @property
    def applied_topic(self) -> str:
        return self._applied_topic

    @property
    def decay_config(self) -> ModelCapsuleDecayConfig:
        return self._decay_config

    def decay_multiplier(self, *, age_days: float) -> float:
        """Decay multiplier in ``[floor, 1.0]`` for an age in days."""
        if age_days <= 0.0:
            return 1.0
        raw_multiplier = math.pow(0.5, age_days / self._decay_config.half_life_days)
        return max(self._decay_config.floor, raw_multiplier)

    def effective_score(
        self,
        raw_score: float,
        *,
        last_scored: datetime,
        now: datetime,
    ) -> float:
        """effective = raw * decay(age); bounded in ``[floor * raw, raw]``."""
        age_seconds = (now - last_scored).total_seconds()
        age_days = max(0.0, age_seconds / _SECONDS_PER_DAY)
        return raw_score * self.decay_multiplier(age_days=age_days)

    def handle(self, input_data: dict[str, object]) -> dict[str, object]:
        """RuntimeLocal handler protocol shim.

        Delegates to :meth:`project` with a ModelCapsuleScoredEvent and a
        DatabaseAdapter supplied as ``input_data['_db']``.
        """
        payload = dict(input_data)
        db_raw = payload.pop("_db", None)
        if not isinstance(db_raw, DatabaseAdapter):
            raise TypeError("handle() requires a DatabaseAdapter in input_data['_db']")
        event_data = {
            key: value for key, value in payload.items() if not key.startswith("_")
        }
        event = ModelCapsuleScoredEvent.model_validate(event_data)
        result = self.project(event, db_raw)
        return result.model_dump(mode="json")

    def project(
        self,
        event: ModelCapsuleScoredEvent,
        db: DatabaseAdapter,
    ) -> ModelProjectionResult:
        """Fold one scored event into the capsule_store projection."""
        identity = event.identity()
        existing = db.query(TABLE, {"capsule_hash": identity.capsule_hash})
        prior_row: dict[str, object] | None = existing[0] if existing else None

        now = datetime.now(tz=UTC).isoformat()
        if prior_row is None:
            prior_hit_count = 0
            created_at = now
        else:
            prior_hit_count = int(str(prior_row["hit_count"]))
            created_at = str(prior_row["created_at"])
        hit_count = prior_hit_count + 1

        # Validate the full record so an empty-effectiveness row can never land.
        record = ModelCapsuleRecord(
            identity=identity,
            effectiveness=event.effectiveness(hit_count=hit_count),
            validity_scope=event.validity_scope,
        )
        row: dict[str, object] = {
            "capsule_id": str(record.identity.capsule_id),
            "capsule_hash": record.identity.capsule_hash,
            "factor": record.identity.factor.value,
            "source_commit": record.identity.source_commit,
            "source_artifact": record.identity.source_artifact,
            "schema_version": record.identity.schema_version.value,
            "validity_scope": record.validity_scope,
            "success_rate": record.effectiveness.success_rate,
            "first_pass_rate": record.effectiveness.first_pass_rate,
            "cost_per_success": record.effectiveness.cost_per_success,
            "hit_count": record.effectiveness.hit_count,
            "last_scored": record.effectiveness.last_scored.isoformat(),
            "created_at": created_at,
            "updated_at": now,
        }
        ok = db.upsert(TABLE, CONFLICT_KEY, row)
        return ModelProjectionResult(rows_upserted=1 if ok else 0)


__all__: list[str] = [
    "CONFLICT_KEY",
    "TABLE",
    "HandlerCapsuleStoreProjection",
    "ModelCapsuleDecayConfig",
    "ModelCapsuleScoredEvent",
    "ModelProjectionResult",
]
