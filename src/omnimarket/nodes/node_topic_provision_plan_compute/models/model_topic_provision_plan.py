# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rule, request and plan for provisioning broker topics on demand (OMN-23614).

A topic is created only for a contract that a running profile loads. The profile
and the lane manifest decide which contracts are loaded (the caller resolves them
and passes the names); the rule below comes from the contract's config block and
the node overlay, never from code.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelTopicProvisionRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    include_subscribe_topics: bool
    include_publish_topics: bool
    default_partitions: int = Field(ge=1)  # partitions a not-yet-created topic gets
    partition_bar: int = Field(ge=1)  # the broker's declared partition bar


class ModelContractTopics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract_name: str
    subscribe_topics: tuple[str, ...] = ()
    publish_topics: tuple[str, ...] = ()


class ModelLiveTopic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    partitions: int = Field(ge=1)


class ModelTopicProvisionPlanRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: ModelTopicProvisionRule
    contracts: tuple[ModelContractTopics, ...]  # every contract the registry holds
    loaded_contract_names: tuple[str, ...]  # contracts the profile and manifest load
    live_topics: tuple[ModelLiveTopic, ...]  # topics on the broker now (list only)


class ModelTopicProvisionPlanResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["planned", "refused"]
    refusal_reasons: tuple[str, ...] = ()
    topics_to_provision: tuple[str, ...] = ()  # sorted: the topics the rule allows
    excluded_contracts: tuple[str, ...] = ()  # registry contracts no profile loads
    contracts_total: int = 0
    contracts_loaded: int = 0
    live_topics_total: int = 0
    live_topics_inside_rule: int = 0  # live and allowed by the rule
    live_topics_outside_rule: int = 0  # live and allowed by no loaded contract
    topics_to_create: int = 0  # allowed by the rule and not live yet
    would_exist_topics: int = 0  # inside the rule plus to-create
    projected_partitions: int = 0  # live partitions inside the rule plus new ones
    partition_bar: int = 0
    under_partition_bar: bool = False
