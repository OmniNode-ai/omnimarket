# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed rule, document and report reading every topic a node contract declares (OMN-19998).

The node is the single reader of a contract's declared topics, in every shape.
It carries no effect: it only reports what the contracts declare.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict


class EnumContractTopicShape(StrEnum):
    EVENT_BUS_SUBSCRIBE = "event_bus_subscribe"
    EVENT_BUS_PUBLISH = "event_bus_publish"
    EVENT_BUS_DLQ = "event_bus_dlq"
    PROJECTION_API = "projection_api"
    CONSUMED_EVENTS = "consumed_events"
    EXTERNALLY_PRODUCED = "externally_produced"
    EXTERNALLY_CONSUMED = "externally_consumed"
    COMMAND = "command"
    TERMINAL = "terminal"
    HANDLER_ROUTING = "handler_routing"
    TOPICS_MAP = "topics_map"
    PUBLISHED_EVENTS = "published_events"


class ModelContractTopicRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    topic_pattern: (
        str  # a regex a topic literal must fully match; comes only from the overlay
    )


class ModelContractDocument(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    path: str
    document: dict[str, Any] | None = None
    read_error: str | None = None


class ModelContractTopicRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    rule: ModelContractTopicRule
    documents: tuple[ModelContractDocument, ...]


class ModelContractTopicUse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: str
    topic: str
    shape: EnumContractTopicShape


class ModelRejectedLiteral(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: str
    shape: EnumContractTopicShape
    literal: str


class ModelUnreadableContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    contract: str
    path: str
    reason: str


class ModelContractTopicReport(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    uses: tuple[ModelContractTopicUse, ...] = ()
    topics_by_shape: dict[str, tuple[str, ...]] = {}
    all_topics: tuple[str, ...] = ()
    rejected: tuple[ModelRejectedLiteral, ...] = ()
    unreadable: tuple[ModelUnreadableContract, ...] = ()
    contracts_read: int = 0
