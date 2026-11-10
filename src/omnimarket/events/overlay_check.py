# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared models of one overlay's verdict against the runtime's installed model.

Re-homed here from ``omnimarket.nodes.node_overlay_drift_effect.models.model_overlay_drift``
so sibling nodes consume the shared event models instead of reaching into another
node's models package (see ``tests/test_no_cross_node_reach_in.py``).
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumOverlayVerdict(StrEnum):
    """One overlay against the runtime's model."""

    ACCEPTED = "accepted"
    #: The installed model refused the document (the rung_order case).
    REJECTED = "rejected"
    #: The pin names a module or class the runtime does not have.
    MODEL_MISSING = "model_missing"
    #: The overlay file is not YAML.
    UNREADABLE = "unreadable"
    #: A pin names an overlay the home does not hold.
    OVERLAY_MISSING = "overlay_missing"


class ModelOverlayCheck(BaseModel):
    """One overlay's verdict; ``fields`` names refused fields, never a value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_id: str
    verdict: EnumOverlayVerdict
    fields: tuple[str, ...] = ()
    error_class: str = ""


__all__ = ["EnumOverlayVerdict", "ModelOverlayCheck"]
