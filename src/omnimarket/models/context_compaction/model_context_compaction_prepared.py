# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The ``-prepared.v1`` receipt: one deterministic assembly, no model-dependent hash (OMN-21113, TA.11)."""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelContextCompactionPrepared(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: UUID
    session_id: str
    input_messages_hash: str
    assembly_hash: str
    version_vector_hash: str
    profile_name: str
    profile_content_hash: str


__all__ = ["ModelContextCompactionPrepared"]
