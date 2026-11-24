# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The audit row of one set: who set which declared key, the value before and the value after.

The model is shared with the store that writes the row (``omnimarket.models.overlay_store``); this module keeps the
name the node's callers import.
"""

from __future__ import annotations

from omnimarket.models.overlay_store import ModelOverlaySettingAudit

__all__ = ["ModelOverlaySettingAudit"]
