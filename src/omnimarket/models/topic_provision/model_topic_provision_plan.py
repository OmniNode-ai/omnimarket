# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The output of the topic provision plan."""

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.topic_provision.enum_topic_provision import (
    EnumTopicProvisionMode,
    EnumTopicSkipReason,
)


class ModelTopicProvisionSkip(BaseModel):
    """One contract that the active rule did not load, with its reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_name: str = Field(min_length=1)
    reason: EnumTopicSkipReason


class ModelTopicProvisionPlan(BaseModel):
    """The topics to provision and the live-topic accounting under the rule."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile: str
    mode: EnumTopicProvisionMode
    topics_to_provision: tuple[str, ...]
    contracts_loaded: tuple[str, ...]
    contracts_skipped: tuple[ModelTopicProvisionSkip, ...]
    declared_topic_count: int
    live_topic_count: int
    live_topics_under_rule: int
    live_topics_outside_rule: int
    live_topics_undeclared: int
