# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The overlay document of the arm comparison node."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.models.delegation_capability_map.model_capability_line import (
    ModelCapabilityLine,
)


class ModelArmCompareOverlay(BaseModel):
    """The overlay key ``delegation.capability_map.line``: the comparison's line.

    The line is the capability map's own class line, reused as-is: its margin,
    confidence and power size the paired test and its floor rides along. No key
    has a default; a deployment that states none is refused, not defaulted.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    line: ModelCapabilityLine
