# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Compatibility export for the shared PR lifecycle triage input model.

Related:
    - OMN-8083: pr_lifecycle_triage_compute
"""

from __future__ import annotations

from omnimarket.events.pr_lifecycle_triage import ModelPrTriageInput

__all__: list[str] = ["ModelPrTriageInput"]
