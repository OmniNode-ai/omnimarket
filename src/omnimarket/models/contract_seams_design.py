# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A design as a graph, the input of node_contract_seams_orchestrator (OMN-21627).

A design lists its nodes, the crossings between them and what each side of a crossing
believes crosses the wire. Each side is declared separately on purpose: a design whose
producer and consumer disagree about a crossing must be refused at the seam, before any
handler is written, and that is only possible when both beliefs are recorded.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from omnimarket.models.model_generate_node import EnumNodeType
from omnimarket.seams.models.model_seam_projection import (
    EnumSeamDeliverySemantics,
    ModelSeamProjectionField,
)

_NODE_NAME = r"^node_[a-z][a-z0-9_]*$"
_TOPIC = r"^onex\.(cmd|evt)\.[a-z0-9-]+\.[a-z0-9-]+\.v[0-9]+$"


class ModelDesignNode(BaseModel):
    """One node of the design, with an optional typed state machine for the walker."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_name: str = Field(pattern=_NODE_NAME)
    archetype: EnumNodeType
    description: str = Field(min_length=1)
    state_machine: dict[str, JsonValue] | None = None


class ModelDesignCrossingSide(BaseModel):
    """What one side of a crossing declares crosses the wire."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    envelope_model: str = Field(min_length=1)
    envelope_version: str = Field(min_length=1)
    key_fields: tuple[ModelSeamProjectionField, ...] = ()
    delivery_semantics: EnumSeamDeliverySemantics = (
        EnumSeamDeliverySemantics.AT_LEAST_ONCE
    )


class ModelDesignCrossing(BaseModel):
    """One crossing between two design nodes, by topic, with each side's declaration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    crossing_id: str = Field(min_length=1)
    topic: str = Field(pattern=_TOPIC)
    producer: str = Field(pattern=_NODE_NAME)
    consumer: str = Field(pattern=_NODE_NAME)
    producer_side: ModelDesignCrossingSide
    consumer_side: ModelDesignCrossingSide


class ModelDesignBoundary(BaseModel):
    """Where the graph meets the world: the topic and the design node on that end."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(pattern=_TOPIC)
    node_name: str = Field(pattern=_NODE_NAME)


class ModelContractSeamsDesign(BaseModel):
    """Nodes, crossings, the head command that starts the chain and the tail it ends at."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    design_id: str = Field(min_length=1)
    nodes: tuple[ModelDesignNode, ...] = Field(min_length=1)
    crossings: tuple[ModelDesignCrossing, ...]
    head: ModelDesignBoundary
    tail: ModelDesignBoundary

    @model_validator(mode="after")
    def _unique_ids(self) -> Self:
        names = [node.node_name for node in self.nodes]
        if len(set(names)) != len(names):
            raise ValueError("design nodes repeat a node_name")
        ids = [crossing.crossing_id for crossing in self.crossings]
        if len(set(ids)) != len(ids):
            raise ValueError("design crossings repeat a crossing_id")
        return self

    def issues(self) -> tuple[str, ...]:
        """Gaps a design must close before a contract is written, one line each."""
        names = {node.node_name for node in self.nodes}
        found: list[str] = []
        for boundary, label in ((self.head, "head"), (self.tail, "tail")):
            if boundary.node_name not in names:
                found.append(f"{label} names {boundary.node_name}, not a design node")
        touched = {self.head.node_name, self.tail.node_name}
        for crossing in self.crossings:
            for end, role in (
                (crossing.producer, "producer"),
                (crossing.consumer, "consumer"),
            ):
                if end not in names:
                    found.append(
                        f"crossing {crossing.crossing_id} {role} {end} is not a design node"
                    )
                touched.add(end)
            if crossing.producer == crossing.consumer:
                found.append(
                    f"crossing {crossing.crossing_id} produces and consumes on one node"
                )
        for name in sorted(names - touched):
            found.append(f"node {name} has no crossing")
        return tuple(found)


__all__ = [
    "ModelContractSeamsDesign",
    "ModelDesignBoundary",
    "ModelDesignCrossing",
    "ModelDesignCrossingSide",
    "ModelDesignNode",
]
