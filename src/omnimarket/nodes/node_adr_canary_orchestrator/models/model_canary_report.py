# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelCanaryReport -- output contract for node_adr_canary_orchestrator.

Canonical definitions live in omnimarket.events.canary (OMN-10845).
This module re-exports them so existing intra-node imports continue to work.

[OMN-10698]
"""

from omnimarket.events.canary import ModelCanaryReport, ModelModelScore

__all__: list[str] = ["ModelCanaryReport", "ModelModelScore"]
