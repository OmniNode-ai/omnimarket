# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay of the delegated code edit loop (OMN-23930).

The round limit, the check time limit and the empty-finish limit of the code
edit loop are values of the node's overlay. Nothing here has a default for an
overlay key: an install with no overlay keeps the request model's own
defaults, and an overlay that is present but invalid raises, never a default.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

CODE_EDIT_LOOP_OVERLAY_ID = "node_delegated_code_edit_orchestrator"


class ModelCodeEditLoopOverlay(BaseModel):
    """The overlay of node_delegated_code_edit_orchestrator. No key has a default."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_turns: int = Field(ge=1, le=40)
    check_timeout_seconds: int = Field(ge=1, le=1800)
    max_no_change_finishes: int = Field(ge=1, le=10)
