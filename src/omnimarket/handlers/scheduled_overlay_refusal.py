# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Latch a missing-overlay refusal once per distinct cause (OMN-23587).

A scheduled effect that raises on every runtime tick replays the raise up to five times to the shared
runtime-tick topic, and every other consumer group logs a skip line per replay. The latch logs one ERROR
line per distinct cause and one INFO line when the overlay later appears, so a later-appearing overlay is
picked up on the next tick without a fresh log per replay.
"""

from __future__ import annotations

import logging

from omnimarket.handlers.node_overlay_reader import NodeOverlayNotFoundError
from omnimarket.models.scheduled_overlay import ModelScheduledOverlayRefusal


class ScheduledOverlayRefusalLatch:
    """Log a missing-overlay refusal once per distinct cause, and once on recovery."""

    def __init__(self, node_name: str, log: logging.Logger | None = None) -> None:
        self._node_name = node_name
        self._log = log if log is not None else logging.getLogger(__name__)
        self._latched: ModelScheduledOverlayRefusal | None = None

    def refuse(
        self, error: NodeOverlayNotFoundError
    ) -> ModelScheduledOverlayRefusal | None:
        """Build the typed refusal; log one ERROR line only when the cause is new."""
        refusal = ModelScheduledOverlayRefusal(
            node_name=self._node_name,
            error_type=type(error).__name__,
            detail=str(error),
        )
        if self._latched is not None and (
            self._latched.error_type == refusal.error_type
            and self._latched.detail == refusal.detail
        ):
            return None
        self._latched = refusal
        self._log.error(
            "scheduled overlay refused: node=%s error_type=%s detail=%s",
            refusal.node_name,
            refusal.error_type,
            refusal.detail,
        )
        return refusal

    def recovered(self) -> bool:
        """Clear a latched refusal and log one INFO line; False and silent when none is latched."""
        if self._latched is None:
            return False
        self._latched = None
        self._log.info(
            "scheduled overlay recovered: node=%s",
            self._node_name,
        )
        return True
