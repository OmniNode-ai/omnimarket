# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The provision rule and the contract config that carries it."""

from pydantic import BaseModel, ConfigDict

from omnimarket.models.topic_provision.enum_topic_provision import (
    EnumTopicProvisionMode,
)


class ModelTopicProvisionRule(BaseModel):
    """The selection mode for the provision plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: EnumTopicProvisionMode


class ModelTopicProvisionConfig(BaseModel):
    """The shape of the contract's ``config:`` section."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: ModelTopicProvisionRule
