# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared model of one overlay's verdict against the runtime's installed model.

Re-homed here from ``omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift``
so sibling nodes consume the shared model instead of reaching into another node's
models package (see ``tests/test_no_cross_node_reach_in.py``).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.enums.enum_overlay_verdict import EnumOverlayVerdict


class ModelOverlayCheck(BaseModel):
    """One overlay's verdict; ``fields`` names refused fields, never a value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_id: str
    verdict: EnumOverlayVerdict
    fields: tuple[str, ...] = ()
    error_class: str = ""


__all__ = ["ModelOverlayCheck"]
