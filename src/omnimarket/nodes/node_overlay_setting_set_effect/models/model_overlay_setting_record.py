# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The record a set puts on the bus: a whole record of the store plus the settings an operator set."""

from __future__ import annotations

from omnimarket.models.overlay_store.model_overlay_store import ModelOverlayStoreRecord


class ModelOverlaySettingRecord(ModelOverlayStoreRecord):
    """A store record that names the setting paths an operator set (the same field and type the refresh keeps)."""

    operator_paths: tuple[str, ...] = ()


__all__ = ["ModelOverlaySettingRecord"]
