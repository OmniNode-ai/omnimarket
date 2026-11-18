# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One overlay's verdict against the runtime's installed model.

Shared by the overlay drift effect and the overlay merge-order compute so neither
node reaches into the other's models package (see ``tests/test_no_cross_node_reach_in.py``).
"""

from __future__ import annotations

from enum import StrEnum


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


__all__ = ["EnumOverlayVerdict"]
