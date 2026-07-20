# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility export for the shared PR lifecycle inventory item model.

This model represents the raw data produced by pr_lifecycle_inventory_compute.
The triage node consumes these items and classifies each one.

Related:
    - OMN-8082: pr_lifecycle_inventory_compute (producer)
    - OMN-8083: pr_lifecycle_triage_compute (consumer)
"""

from __future__ import annotations

from omnimarket.events.pr_lifecycle_triage import ModelPrInventoryItem

__all__: list[str] = ["ModelPrInventoryItem"]
