# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The topic provision rule is a deployment fact (OMN-23614).

Which topic kinds count toward the plan, the partitions a not-yet-created topic gets
and the broker's partition bar are decided by whoever runs the system, never by this
package. The rule is read only from the node overlay,
``<root>/node_topic_provision_plan_compute/overlay.yaml`` under the first root named
in ``ONEX_SKILL_OVERLAY_ROOTS``; there is no packaged fallback, so a deployment that
ships no overlay gets :class:`NodeOverlayNotFoundError` instead of a guessed rule.
"""

from __future__ import annotations

from collections.abc import Mapping

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_plan import (
    ModelTopicProvisionRule,
)

NODE_NAME = "node_topic_provision_plan_compute"


def load_provision_rule(
    environ: Mapping[str, str] | None = None,
) -> ModelTopicProvisionRule:
    """Read the provision rule from the node overlay, refusing when none exists."""
    return load_structured_overlay(NODE_NAME, ModelTopicProvisionRule, environ)
