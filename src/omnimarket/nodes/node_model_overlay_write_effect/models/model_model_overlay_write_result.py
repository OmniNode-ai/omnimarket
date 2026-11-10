# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What the model overlay write effect did. Never carries a key (OMN-20330)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from omnimarket.nodes.node_model_overlay_write_effect.models.model_model_overlay_write_request import (
    OverlayMode,
)


class ModelModelOverlayWriteResult(BaseModel):
    """``written``: the file now holds the overlay. ``unchanged``: it already did.
    ``refused``: nothing was written, and ``reason`` says why (a file the
    developer wrote, the lab stack's out-of-MVP own key, an overlay that would
    carry a key reference)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: OverlayMode
    outcome: Literal["written", "unchanged", "refused"]
    path: str | None = None
    backend_ids: tuple[str, ...] = ()
    served_model_id: str | None = None
    reason: str | None = None


__all__ = ["ModelModelOverlayWriteResult"]
