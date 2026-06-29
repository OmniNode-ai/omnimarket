# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Contract-referenced I/O for node_github_diff_effect (OMN-13210 / B1).

The command/event models are OWNED by the shared ``omnimarket.review.node_io``
module so the orchestrator and this node import them from one place without a
cross-node reach-in. This module re-exports them at the contract-declared path.
"""

from __future__ import annotations

from omnimarket.review.node_io import (
    ModelGithubDiffCommand,
    ModelGithubDiffResolvedEvent,
)

__all__: list[str] = [
    "ModelGithubDiffCommand",
    "ModelGithubDiffResolvedEvent",
]
