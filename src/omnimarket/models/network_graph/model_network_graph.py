# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, document, vertex, edge, overlay and dialect models of the network graph.

The compute node reads contract texts and returns the structure of the graph:
vertices keyed repo/name and one edge per (publisher, topic, subscriber). Every
list in the document is sorted, so one request always gives one set of bytes.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.network_graph.enum_network_graph import (
    EnumDialectShape,
    EnumTopicDirection,
    EnumVertexCause,
    EnumVertexState,
)


class ModelDialect(BaseModel):
    """One place a contract may declare topics, and in which direction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    direction: EnumTopicDirection
    path: tuple[str, ...] = Field(min_length=1)
    shape: EnumDialectShape


class ModelDeclaredTopics(BaseModel):
    """The topics one contract declares, per direction."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    publishes: frozenset[str] = frozenset()
    subscribes: frozenset[str] = frozenset()


class ModelContractText(BaseModel):
    """One contract file: its repo, its repo-relative path and its text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    path: str = Field(min_length=1)
    text: str


class ModelNetworkGraphOverlay(BaseModel):
    """The values the compute node is handed; none is held in its code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command_segment: str = Field(min_length=1)
    domain_segment_index: int = Field(ge=0)
    window: timedelta


class ModelNetworkGraphRequest(BaseModel):
    """Contract texts, the overlay and the reference time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    contracts: tuple[ModelContractText, ...]
    overlay: ModelNetworkGraphOverlay
    reference_time: datetime


class ModelNetworkGraphVertex(BaseModel):
    """One node contract. state and cause stay None until a state rule sets them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    key: str
    repo: str
    name: str
    path: str
    node_type: str
    publishes: tuple[str, ...] = ()
    subscribes: tuple[str, ...] = ()
    state: EnumVertexState | None = None
    cause: EnumVertexCause | None = None


class ModelNetworkGraphEdge(BaseModel):
    """A declared pairing: publisher and subscriber share the topic."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    publisher: str
    topic: str
    subscriber: str


class ModelNetworkGraphDocument(BaseModel):
    """The structure of the network graph at one reference time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    generated_at: datetime
    overlay: ModelNetworkGraphOverlay
    vertices: tuple[ModelNetworkGraphVertex, ...] = ()
    edges: tuple[ModelNetworkGraphEdge, ...] = ()

    def digest(self) -> str:
        """The sha256 of the document's JSON bytes, the profile-parity comparison."""
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()
