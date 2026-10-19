# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Payload of ``onex.cmd.omniintelligence.context-pack-built.v1``.

The package text travels in ``messages``, so this result is published on the restricted
``onex.cmd.omniintelligence.*`` family only, never on an ``onex.evt.*`` topic.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.enums.enum_context_pack_failure import EnumContextPackFailure
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.context_pack_orchestrator.enum_context_pack_orchestrator_status import (
    EnumContextPackOrchestratorStatus,
)
from omnimarket.models.context_pack_orchestrator.model_context_pack_tier_status import (
    ModelContextPackTierStatus,
)
from omnimarket.models.context_pack_render import (
    EnumContextPackRenderFailure,
    ModelContextPackRenderMessage,
)


class ModelContextPackBuilt(BaseModel):
    """The rendered package and its section 6 identities, or a typed failure."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: EnumContextPackOrchestratorStatus
    profile_name: str
    profile_version: str | None = None
    profile_content_hash: str | None = None
    task_kind: str | None = None
    candidate_id: str | None = None
    assembly_hash: str | None = None
    final_pack_hash: str | None = None
    body_hash: str | None = None
    stable_prefix_hash: str | None = None
    version_vector_hash: str | None = None
    generated_at: str | None = None
    messages: tuple[ModelContextPackRenderMessage, ...] = ()
    factor_token_estimates: Mapping[EnumContextFactor, int] = Field(
        default_factory=dict
    )
    tier_statuses: tuple[ModelContextPackTierStatus, ...] = ()
    excluded_by_budget: tuple[EnumContextFactor, ...] = ()
    model_outputs: tuple[str, ...] = ()
    implementation_identity: Mapping[str, str] = Field(default_factory=dict)
    failure_class: EnumContextPackFailure | None = None
    render_failure_class: EnumContextPackRenderFailure | None = None
    errors: tuple[str, ...] = ()


__all__ = ["ModelContextPackBuilt"]
