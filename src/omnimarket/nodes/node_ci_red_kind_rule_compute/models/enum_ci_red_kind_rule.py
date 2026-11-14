# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

from __future__ import annotations

from enum import StrEnum


class EnumCiRedKindRuleRefusal(StrEnum):
    OVERLAY_UNBOUND = "overlay_unbound"
    OVERLAY_INVALID = "overlay_invalid"


__all__: list[str] = ["EnumCiRedKindRuleRefusal"]
