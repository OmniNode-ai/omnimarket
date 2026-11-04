# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed input and output of the generic projection-write effect node (OMN-18883).

A projection is a pure fold plus a write. The fold is a definition-B handler
that returns :class:`ModelProjectionFoldResult`: the table, its conflict key and
the rows it decided. The write is ``node_projection_write_effect``, which takes
that result together with :class:`ModelProjectionEnvelopeMetadata` as one typed
:class:`ModelProjectionWriteRequest`. The database handle is the effect
handler's constructor dependency, never a payload key, and the envelope
metadata is a typed field, never ``_db``/``_topic``/``_tenant_id`` keys injected
into an event dict.

This module imports no database adapter, so a fold that returns these models
stays free of I/O.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "ModelProjectionEnvelopeMetadata",
    "ModelProjectionFoldResult",
    "ModelProjectionWriteRequest",
    "ModelProjectionWriteResult",
    "ProjectionCellValue",
]

#: A value a fold may place in one projected column. ``datetime`` and ``UUID``
#: are listed so a TIMESTAMPTZ or UUID column is bound as its own type, not as a
#: string asyncpg refuses (the OMN-15905 defect class).
ProjectionCellValue = (
    datetime | UUID | bool | int | float | str | None | dict[str, object] | list[object]
)


class ModelProjectionEnvelopeMetadata(BaseModel):
    """The envelope facts a projection write carries across the effect boundary."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    envelope_id: str = Field(..., min_length=1)
    event_type: str = Field(..., min_length=1)
    topic: str = Field(..., min_length=1)
    envelope_timestamp: datetime
    tenant_id: str | None = None


class ModelProjectionFoldResult(BaseModel):
    """What a pure projection fold decided: rows for one table, keyed for UPSERT."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: str = Field(..., min_length=1)
    conflict_key: str = Field(
        ..., min_length=1, description="Comma-separated conflict columns."
    )
    rows: tuple[dict[str, ProjectionCellValue], ...] = ()
    insert_only_columns: tuple[str, ...] = ()
    jsonb_columns: tuple[str, ...] = ()

    @field_validator("rows")
    @classmethod
    def _rows_non_empty_mappings(
        cls, value: tuple[dict[str, ProjectionCellValue], ...]
    ) -> tuple[dict[str, ProjectionCellValue], ...]:
        if any(not row for row in value):
            raise ValueError("a projected row must name at least one column")
        return value


class ModelProjectionWriteRequest(BaseModel):
    """The one typed request of ``node_projection_write_effect``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    fold_result: ModelProjectionFoldResult
    envelope: ModelProjectionEnvelopeMetadata


class ModelProjectionWriteResult(BaseModel):
    """What the write did. ``rows_upserted`` is the key the runtime counts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    table: str
    envelope_id: str
    rows_upserted: int = Field(..., ge=0)
