# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Payload of ``onex.cmd.omniintelligence.context-pack-requested.v1`` (plan section 4 item 5).

The payload carries no tenant: the tenant is read from the command envelope and paired
with this payload in ``ModelContextPackCommand``. ``extra="forbid"`` refuses a tenant
field placed here.
"""

from __future__ import annotations

from uuid import UUID

from omnibase_core.enums.enum_context_factor import EnumContextFactor
from omnibase_core.models.context.model_context_caller_scope import (
    ModelContextCallerScope,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.enums.enum_context_binding_kind import EnumContextBindingKind
from omnimarket.models.context_pack_orchestrator.enum_context_pack_mode import (
    EnumContextPackMode,
)


class ModelContextPackRequest(BaseModel):
    """What to build: a profile by name over a caller scope, with or without a session.

    ``session_id`` and ``newest_turn`` are optional, so a profile without session tiers
    builds without a session. ``token_budget_override`` may only lower the profile's
    budget; a higher value fails the build as ``INVALID_PROFILE``.

    ``tier_revisions`` names the revision one tier's source is read at (a ledger row
    stamp for the ledger source, a transcript-chain sha256 for the pinned session
    source); a tier it does not name reads the request-wide revision. ``cited_refs``
    names the entries (by ``source_ref`` or ledger row stamp) a required tier over
    its budget keeps first; a cited entry is never trimmed.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_name: str = Field(min_length=1)
    caller_scope: ModelContextCallerScope = Field(
        default_factory=ModelContextCallerScope
    )
    session_id: str | None = Field(default=None, min_length=1)
    newest_turn: str | None = Field(default=None, min_length=1)
    session_barrier: str | None = Field(default=None, min_length=1)
    token_budget_override: int | None = Field(default=None, gt=0)
    mode: EnumContextPackMode = EnumContextPackMode.LIVE
    manifest_case: str | None = Field(default=None, min_length=1)
    tier_revisions: dict[EnumContextFactor, str] = Field(default_factory=dict)
    cited_refs: tuple[str, ...] = ()
    # An additive binding (a Claude Code hook) names itself and its invocation (OMN-22016):
    # the answer then carries a client verdict, and only an enabled profile is built.
    binding_kind: EnumContextBindingKind | None = None
    invocation_id: UUID | None = None

    @model_validator(mode="after")
    def _pinned_names_its_case(self) -> ModelContextPackRequest:
        if (self.mode is EnumContextPackMode.PINNED) != (
            self.manifest_case is not None
        ):
            raise ValueError("manifest_case is required in pinned mode and only there")
        if self.newest_turn is not None and self.session_id is None:
            raise ValueError("newest_turn requires session_id")
        if any(not revision for revision in self.tier_revisions.values()):
            raise ValueError("a tier revision may not be empty")
        if (self.binding_kind is EnumContextBindingKind.ADDITIVE) != (
            self.invocation_id is not None
        ):
            raise ValueError(
                "invocation_id is required for an additive call and only there"
            )
        return self


__all__ = ["ModelContextPackRequest"]
