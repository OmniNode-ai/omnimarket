# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of the delegated code edit loop (OMN-23930).

The round limit, the check time limit, the empty-finish limit and the wall
budget of the code edit loop are values of the node's overlay. The wall budget
is the one optional key: an overlay that does not name it leaves the request
without a limit (an overlay written before this key existed stays valid).
Nothing here has a default for an overlay key: an install with no overlay
keeps the request model's own defaults, and an overlay that is present but
invalid raises, never a default.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

CODE_EDIT_LOOP_OVERLAY_ID = "node_delegated_code_edit_orchestrator"


class ModelCodeEditLoopOverlay(BaseModel):
    """The overlay of node_delegated_code_edit_orchestrator.

    Three required keys; wall_budget_seconds is the one optional key, and an
    overlay that omits it leaves the request without a wall limit.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_turns: int = Field(ge=1, le=40)
    check_timeout_seconds: int = Field(ge=1, le=1800)
    max_no_change_finishes: int = Field(ge=1, le=10)
    wall_budget_seconds: int | None = Field(default=None, ge=1, le=7200)
