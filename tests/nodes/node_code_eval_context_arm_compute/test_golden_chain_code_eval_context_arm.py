# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""OMN-23629: the golden-chain coverage entry of node_code_eval_context_arm_compute.

The golden chains (exemplar arm and full-pack arm, select, build and judge over the bus)
live in test_chains.py beside the node's error chains; this module is the name the
golden-chain coverage gate looks for and runs the same two chains.
"""

from __future__ import annotations

from .test_chains import (
    test_golden_chain_exemplar_arm_selects_builds_and_judges,
    test_golden_chain_pack_arm_selects_builds_and_judges,
)

__all__ = [
    "test_golden_chain_exemplar_arm_selects_builds_and_judges",
    "test_golden_chain_pack_arm_selects_builds_and_judges",
]
