# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The request of one read-only provision dry run."""

from pydantic import BaseModel, ConfigDict

from omnimarket.models.topic_provision.model_topic_provision_overlay import (
    ModelTopicProvisionOverlay,
)
from omnimarket.models.topic_provision.model_topic_provision_rule import (
    ModelTopicProvisionRule,
)


class ModelTopicProvisionDryRunRequest(BaseModel):
    """The overlay (profile, lane manifest, contract roots) and the rule to apply."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    overlay: ModelTopicProvisionOverlay
    rule: ModelTopicProvisionRule
