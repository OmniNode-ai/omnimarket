# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Settings of the ticket hand edit alarm effect, read at evaluation time.

Settings come from ``<root>/node_ticket_hand_edit_alarm_effect/overlay.yaml`` under
``ONEX_SKILL_OVERLAY_ROOTS``. Nothing is packaged, and nothing reads the overlay
while the runtime wires the node: a missing overlay refuses the evaluation.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_ticket_hand_edit_alarm_effect.models import (
    ModelTicketHandEditSettings,
)

OVERLAY_ID = NODE_NAME = "node_ticket_hand_edit_alarm_effect"


def load_settings(
    environ: Mapping[str, str] | None = None,
) -> ModelTicketHandEditSettings:
    """Raise ``NodeOverlayError`` naming the setting when the overlay is absent or invalid."""
    return load_structured_overlay(NODE_NAME, ModelTicketHandEditSettings, environ)


__all__ = ["NODE_NAME", "OVERLAY_ID", "load_settings"]
