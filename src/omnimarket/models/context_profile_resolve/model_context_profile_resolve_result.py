# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Result: the resolved profile with its content hash, or a typed failure."""

from __future__ import annotations

from enum import StrEnum

from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.pack.model_context_profile import ModelContextProfile


class EnumContextProfileResolveStatus(StrEnum):
    """Outcome of resolving a profile by name."""

    OK = "ok"
    FAILED = "failed"


class ModelContextProfileResolveResult(BaseModel):
    """``profile_content_hash`` is sha256 over the profile's canonical dump."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumContextProfileResolveStatus
    profile_name: str
    profile: ModelContextProfile | None = None
    profile_content_hash: str | None = None
    failure_class: EnumContextPackFailure | None = None
    errors: tuple[str, ...] = Field(default_factory=tuple)


__all__ = ["EnumContextProfileResolveStatus", "ModelContextProfileResolveResult"]
