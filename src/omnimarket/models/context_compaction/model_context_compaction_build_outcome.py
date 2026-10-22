# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the context-pack build returns to the compaction orchestrator (OMN-21113, TA.11).

Exactly one of a built and rendered package or a typed failure class. A built
package names its deterministic assembly (``candidate_id``, ``assembly_hash``)
apart from its model output (``model_outputs_hash``) and the final package
(``final_pack_hash``, ``body_hash``).
"""

from __future__ import annotations

from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.context_compaction.enum_context_compaction_failure_class import (
    EnumContextCompactionFailureClass,
)
from omnimarket.models.context_pack_render.model_context_pack_render_message import (
    ModelContextPackRenderMessage,
)

_SHA256 = r"^[0-9a-f]{64}$"


class ModelContextCompactionBuiltPackage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_id: UUID
    assembly_hash: str = Field(pattern=_SHA256)
    model_outputs_hash: str = Field(pattern=_SHA256)
    final_pack_hash: str = Field(pattern=_SHA256)
    body_hash: str = Field(pattern=_SHA256)
    version_vector_hash: str = Field(pattern=_SHA256)
    profile_name: str = Field(min_length=1)
    profile_content_hash: str = Field(pattern=_SHA256)
    messages: tuple[ModelContextPackRenderMessage, ...] = Field(min_length=1)
    #: The model-written section's share of the build, from ModelContextPackBuilt.model_section_ms().
    model_section_ms: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ModelContextCompactionBuildOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    package: ModelContextCompactionBuiltPackage | None = None
    failure_class: EnumContextCompactionFailureClass | None = None

    @model_validator(mode="after")
    def _built_or_failed(self) -> Self:
        if (self.package is None) == (self.failure_class is None):
            raise ValueError(
                "an outcome is a built package or a failure class, not both"
            )
        return self


__all__ = ["ModelContextCompactionBuildOutcome", "ModelContextCompactionBuiltPackage"]
