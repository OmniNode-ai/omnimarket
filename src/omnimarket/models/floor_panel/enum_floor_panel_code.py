# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The refusal codes the floor panel names when a request or its entry cannot be answered."""

from __future__ import annotations

from enum import StrEnum


class EnumFloorPanelCode(StrEnum):
    REQUEST_INVALID = "floor.panel.request_invalid"
    UNKNOWN = "floor.panel.unknown"
    ENTRY_INVALID = "floor.panel.entry_invalid"
    STORE_UNAVAILABLE = "floor.panel.store_unavailable"


__all__ = ["EnumFloorPanelCode"]
