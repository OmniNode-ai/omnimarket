# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay the code edit loop's ports are built from (OMN-24272).

When a runtime message, not ``onex code-edit run``, starts the loop, the onex
path, the state root, the delegate lane and its in-process switch are values
of the node's ports overlay. The token cap is the one optional key: an
overlay that does not name it leaves the delegate without a cap. Nothing here
has a default for an overlay key: an install with no overlay leaves the loop
without ports (a runtime message that starts it is refused), and an overlay
that is present but invalid raises, never a default.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

CODE_EDIT_PORTS_OVERLAY_ID = "node_delegated_code_edit_orchestrator_ports"


class ModelCodeEditPortsOverlay(BaseModel):
    """The ports overlay of node_delegated_code_edit_orchestrator.

    Four required keys; max_tokens is the one optional key, and an overlay
    that omits it leaves the delegate without a token cap.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    onex_path: str = Field(min_length=1)
    state_root: str = Field(min_length=1)
    delegate_lane: str = Field(min_length=1)
    delegate_in_process: bool
    max_tokens: int | None = Field(default=None, ge=1)
