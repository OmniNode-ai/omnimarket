# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Lane-brief pack and delegation refusals."""

from omnimarket.enums.enum_lane_brief_tier import EnumLaneBriefRefusal


class LaneBriefContextRefusedError(ValueError):
    """A pack cannot be built under the lane-brief profile."""

    def __init__(
        self,
        message: str,
        kind: EnumLaneBriefRefusal = EnumLaneBriefRefusal.INVALID_REQUEST,
    ) -> None:
        super().__init__(message)
        self.kind = kind


class LaneBriefRungError(RuntimeError):
    """The delegation rung did not return a usable answer."""
