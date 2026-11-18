# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import (
    load_structured_overlay,
)
from omnimarket.nodes.node_hook_tool_outcome_compute.models.model_hook_tool_outcome import (
    ModelToolOutcomeRules,
)

NODE_NAME = "node_hook_tool_outcome_compute"


def load_outcome_rules(
    environ: Mapping[str, str] | None = None,
) -> ModelToolOutcomeRules:
    """Read the tool outcome rules from the deployment overlay.

    The overlay is ``<root>/<node_name>/overlay.yaml``. An absent overlay
    refuses with an error naming the setting. No default rules in code.
    """
    return load_structured_overlay(NODE_NAME, ModelToolOutcomeRules, environ)
