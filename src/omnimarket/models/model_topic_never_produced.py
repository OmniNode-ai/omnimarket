# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rule, evidence and report listing never-produced broker topics (OMN-23614).

The node only lists. It carries no delete: deletion is a separate effect behind
the operator's go.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelNeverProducedRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    protected_prefixes: tuple[str, ...]  # broker-internal topics never listed
    list_declared_producers: (
        bool  # list a never-produced topic that a contract declares a producer for
    )


class ModelTopicEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    partitions: int = Field(ge=1)
    high_watermark_total: int | None  # None: the offsets were not read
    records_in_retention: int | None  # None: the offsets were not read
    consumer_groups: tuple[str, ...] | None  # None: the groups were not read
    declared_producers: tuple[
        str, ...
    ] = ()  # contracts declaring this topic as published


class ModelNeverProducedRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: ModelNeverProducedRule
    topics: tuple[ModelTopicEvidence, ...]


class ModelNeverProducedTopic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    partitions: int
    evidence: tuple[str, ...]  # plain statements: no producer record, no group, no data


class ModelNotListedTopic(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    reason: Literal[
        "protected",
        "produced",
        "data_in_retention",
        "has_consumer_group",
        "declared_producer",
        "evidence_unread",
    ]


class ModelNeverProducedReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    listed: tuple[ModelNeverProducedTopic, ...] = ()
    not_listed: tuple[ModelNotListedTopic, ...] = ()
    topics_read: int = 0
    deletes_issued: Literal[0] = 0  # this node never deletes
