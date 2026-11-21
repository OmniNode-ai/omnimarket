# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The input to the topic provision plan."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.topic_provision.model_topic_provision_contract import (
    ModelTopicProvisionContract,
)
from omnimarket.models.topic_provision.model_topic_provision_rule import (
    ModelTopicProvisionRule,
)
from omnimarket.models.topic_provision.model_topic_provision_write_grant import (
    ModelTopicProvisionWriteGrant,
)


class ModelTopicProvisionRequest(BaseModel):
    """The contracts, active profile, lane manifest, live topics and declared service logins to plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contracts: tuple[ModelTopicProvisionContract, ...]
    profile: str = Field(min_length=1)
    lane_manifest: tuple[str, ...] = ()
    live_topics: tuple[str, ...] = ()
    rule: ModelTopicProvisionRule
    service_logins: tuple[str, ...] = ()
    acl_write_grants: tuple[ModelTopicProvisionWriteGrant, ...] = ()
