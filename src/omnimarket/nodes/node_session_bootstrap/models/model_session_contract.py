# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local definition of ModelSessionContract for node_session_bootstrap.

Mirrors the wire type in omnibase_compat.overseer.model_session_contract.
When omnibase_compat >= 0.4.0 ships this model, this file can be removed and
the import updated to point at the canonical location.
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class ModelSessionContract(BaseModel, frozen=True, extra="forbid"):
    """Session-level verification contract for autonomous sessions.

    Read by node_session_bootstrap at session start to configure timers,
    phase expectations, and advisory cost ceilings. Frozen and extra-forbid
    for schema safety.

    Rev 7 additions (all have defaults — backward-compatible):
      session_mode, active_sprint_id, model_routing_preference
    """

    session_id: str
    session_label: str
    phases_expected: list[str]
    max_cycles: int = 0
    cost_ceiling_usd: float = Field(default=10.0, ge=0.0)
    halt_on_build_loop_failure: bool = True
    dry_run: bool = False
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    schema_version: str = "1.0"  # string-version-ok: wire envelope field mirrors omnibase_compat overseer wire type
    # Rev 7 fields
    session_mode: str = Field(default="build", pattern="^(build|close-out|reporting)$")
    active_sprint_id: str = "auto-detect"
    model_routing_preference: str = Field(
        default="local-first", pattern="^(local-first|frontier-only|hybrid)$"
    )


__all__: list[str] = ["ModelSessionContract"]
