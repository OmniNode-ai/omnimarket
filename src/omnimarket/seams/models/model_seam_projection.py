# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""``seam-projection/v1`` — the canonical wire-crossing projection of one side
of a seam edge (OMN-15763).

Per the ticket's serialization ruling ("serialize the contracts, use them for
comparison", 2026-08-08): a seam match is NOT bespoke comparison logic over
the whole contract. Each side of an edge is reduced to only the fields that
cross the wire — topic (including the tenant-prefix rule), envelope model +
version, key field names/types, and delivery semantics — then canonically
serialized (schema-versioned, sorted keys) so a diff of two serializations
*is* the match verdict. Hashing the whole contract would churn on irrelevant
edits (timeouts, descriptions); this projection is deliberately narrow.

``schema_version`` is part of the frozen shape so a projection-schema bump
changes the canonical bytes and therefore every pinned hash by design (a
silent schema drift can never masquerade as an unpinned seam).
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "EnumSeamDeliverySemantics",
    "EnumSeamProjectionRole",
    "ModelSeamProjection",
    "ModelSeamProjectionField",
]


class EnumSeamProjectionRole(StrEnum):
    """Which side of the seam edge this projection represents."""

    PRODUCER = "producer"
    CONSUMER = "consumer"


class EnumSeamDeliverySemantics(StrEnum):
    """Delivery guarantee declared or observed for the wire crossing."""

    AT_LEAST_ONCE = "at_least_once"
    AT_MOST_ONCE = "at_most_once"
    EXACTLY_ONCE = "exactly_once"
    UNKNOWN = "unknown"


class ModelSeamProjectionField(BaseModel):
    """One key field carried by the envelope, name + declared type only."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    field_type: str = Field(min_length=1)


class ModelSeamProjection(BaseModel):
    """The canonical wire-crossing projection of one side of a seam edge.

    Only fields that actually cross the wire are represented. ``topic`` is
    the fully resolved wire string (tenant-prefix rule already applied, e.g.
    ``tenant-{slug}.onex.cmd.omnibase-infra.delegation-request.v1`` is
    expressed as the literal template the side asserts — not the bare
    canonical topic with a separate "prefixed: true" flag, because the
    tenant-prefix rule IS part of what must match byte-for-byte between
    producer and consumer per OMN-15757).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["seam-projection/v1"] = "seam-projection/v1"
    edge_id: str = Field(min_length=1)
    role: EnumSeamProjectionRole
    topic: str = Field(min_length=1)
    envelope_model: str = Field(min_length=1)
    envelope_version: str = Field(min_length=1)
    key_fields: tuple[ModelSeamProjectionField, ...] = Field(default_factory=tuple)
    delivery_semantics: EnumSeamDeliverySemantics = EnumSeamDeliverySemantics.UNKNOWN
