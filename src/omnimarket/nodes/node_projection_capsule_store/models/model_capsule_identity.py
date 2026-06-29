# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Stable capsule identity model (OMN-12842 / M2).

A capsule is identified by a deterministic natural key:

    capsule_hash = sha256(canonical(factor, content, source_artifact,
                                    source_commit, schema_version))

A changed exemplar (different content / commit / artifact / schema_version)
is a NEW capsule row, never an in-place mutation of effectiveness. The
surrogate ``capsule_id`` UUID is derived deterministically from the natural
key (UUIDv5) so replay produces the same surrogate, keeping the projection
replay-safe.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from uuid import UUID, uuid5

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from pydantic import BaseModel, ConfigDict, Field

# Stable namespace UUID for capsule surrogate-key derivation. Constant so that
# the same natural key always maps to the same capsule_id across processes and
# replays. Do not change without a schema-version bump.
_CAPSULE_NAMESPACE = UUID("c4b5e0d2-1284-5000-8000-000000000000")


class EnumCapsuleSchemaVersion(StrEnum):
    """Schema version for a stored capsule.

    The version participates in the capsule_hash so a schema change yields a
    new capsule row rather than silently reinterpreting an old one.
    """

    V1 = "v1"


class ModelCapsuleIdentity(BaseModel):
    """Deterministic identity for a stored capsule.

    capsule_id: surrogate UUID PK (UUIDv5 over the natural key).
    capsule_hash: deterministic natural key (sha256 hex over canonical
        provenance fields).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", from_attributes=True)

    capsule_id: UUID = Field(description="Surrogate UUID primary key.")
    capsule_hash: str = Field(
        min_length=64,
        max_length=64,
        description="sha256 hex of the canonical provenance payload.",
    )
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

    @staticmethod
    def _canonical_payload(
        *,
        factor: EnumContextFactor,
        content: str,
        source_artifact: str,
        source_commit: str,
        schema_version: EnumCapsuleSchemaVersion,
    ) -> str:
        """Deterministic JSON over the identity-bearing provenance fields."""
        return json.dumps(
            {
                "factor": factor.value,
                "content": content,
                "source_artifact": source_artifact,
                "source_commit": source_commit,
                "schema_version": schema_version.value,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    def canonical_payload(self) -> str:
        """Return the canonical JSON used to derive this identity's hash."""
        return self._canonical_payload(
            factor=self.factor,
            content=self.content,
            source_artifact=self.source_artifact,
            source_commit=self.source_commit,
            schema_version=self.schema_version,
        )

    @classmethod
    def from_provenance(
        cls,
        *,
        factor: EnumContextFactor,
        content: str,
        source_artifact: str,
        source_commit: str,
        schema_version: EnumCapsuleSchemaVersion,
    ) -> ModelCapsuleIdentity:
        """Build a deterministic identity from provenance fields."""
        payload = cls._canonical_payload(
            factor=factor,
            content=content,
            source_artifact=source_artifact,
            source_commit=source_commit,
            schema_version=schema_version,
        )
        capsule_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        capsule_id = uuid5(_CAPSULE_NAMESPACE, capsule_hash)
        return cls(
            capsule_id=capsule_id,
            capsule_hash=capsule_hash,
            factor=factor,
            content=content,
            source_artifact=source_artifact,
            source_commit=source_commit,
            schema_version=schema_version,
        )


__all__ = [
    "EnumCapsuleSchemaVersion",
    "ModelCapsuleIdentity",
]
