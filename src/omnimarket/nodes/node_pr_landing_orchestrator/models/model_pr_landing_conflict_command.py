# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The orchestrator's conflict request to node_pr_lifecycle_fix_effect."""

from __future__ import annotations

from omnimarket.events.pr_lifecycle_fix.model_fix_command import (
    ModelPrLifecycleFixCommand,
)


class ModelPrLandingConflictCommand(ModelPrLifecycleFixCommand):
    """The orchestrator's request to the fix effect with block_reason conflict.

    Its wire payload is exactly the fix command's. The runtime routes published
    events by class name: the fix command already routes companion commands to
    occ-autobind, while this class routes to
    ``onex.cmd.omnimarket.pr-lifecycle-fix-start.v1``, the fix effect's command
    topic, where node_fixer_dispatcher also routes a conflicted PR.
    """


__all__: list[str] = ["ModelPrLandingConflictCommand"]
