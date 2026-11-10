# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed models of the overlay merge order check: the command, each ahead overlay, the decision (OMN-22809).

The per-overlay answer is node_overlay_drift_effect's own ``ModelOverlayCheck``, so the two nodes cannot
disagree about its shape: a change to it fails this node's seam test.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.events.overlay_check import ModelOverlayCheck

OVERLAY_ID_PATTERN = r"^[a-z0-9_]+$"


class EnumMergeOrderVerdict(StrEnum):
    ORDER_OK = "ORDER_OK"
    AHEAD_OF_CODE = "AHEAD_OF_CODE"


class ModelOverlayMergeOrderCommand(BaseModel):
    """Decide one change: the overlays it touches against the merged tree's answers."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str = Field(min_length=1)
    #: The omnimarket ref the answers were taken at (the merged branch), echoed so a decision names what it judged.
    merged_ref: str = Field(min_length=1)
    #: The overlay ids the change adds or edits. An empty list is refused, never passed.
    changed_overlays: tuple[str, ...] = ()
    #: The merged tree's answer for every overlay of the home, as node_overlay_drift_effect gives it.
    merged_checks: tuple[ModelOverlayCheck, ...] = ()


class ModelAheadOverlay(BaseModel):
    """One touched overlay the merged tree does not accept; ``fields`` names refused fields, never a value."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay_id: str
    verdict: str
    fields: tuple[str, ...] = ()


class ModelOverlayMergeOrderResult(BaseModel):
    """The decision, or the typed failure that refused the check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: str
    status: Literal["completed", "failed"]
    failure: str | None = None
    verdict: EnumMergeOrderVerdict | None = None
    merged_ref: str = ""
    changed: int = 0
    accepted: tuple[str, ...] = ()
    #: Every ahead overlay (the event names at most the overlay's ``max_named``).
    ahead: tuple[ModelAheadOverlay, ...] = ()
    injected_fault: str = ""


__all__ = [
    "OVERLAY_ID_PATTERN",
    "EnumMergeOrderVerdict",
    "ModelAheadOverlay",
    "ModelOverlayMergeOrderCommand",
    "ModelOverlayMergeOrderResult",
]
