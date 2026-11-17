# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""ModelSessionContract for node_session_bootstrap.

This is the node's own frozen model of the session contract. omnibase_core
defines a ModelSessionContract of its own at
``src/omnibase_core/models/overseer/model_session_contract.py``; this module is
not that type's home.
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
    # string-version-ok: wire envelope field of the session contract
    schema_version: str = "1.0"
    # Rev 7 fields
    session_mode: str = Field(default="build", pattern="^(build|close-out|reporting)$")
    active_sprint_id: str = "auto-detect"
    model_routing_preference: str = Field(
        default="local-first", pattern="^(local-first|frontier-only|hybrid)$"
    )


__all__: list[str] = ["ModelSessionContract"]
