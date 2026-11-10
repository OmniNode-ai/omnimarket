# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The input to the topic provision plan."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_contract import (
    ModelTopicProvisionContract,
)
from omnimarket.nodes.node_topic_provision_plan_compute.models.model_topic_provision_rule import (
    ModelTopicProvisionRule,
)


class ModelTopicProvisionRequest(BaseModel):
    """The contracts, active profile, lane manifest and live topics to plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contracts: tuple[ModelTopicProvisionContract, ...]
    profile: str = Field(min_length=1)
    lane_manifest: tuple[str, ...] = ()
    live_topics: tuple[str, ...] = ()
    rule: ModelTopicProvisionRule
