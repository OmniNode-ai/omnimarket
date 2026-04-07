# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Seam boundary models for contract-driven ticket buildability.

A ticket's seam boundaries declare what protocols it consumes, what it
produces, and what topics/tables it touches. The classifier uses this
to determine buildability: if all consumed protocols are mockable,
the ticket is auto-buildable regardless of upstream completion.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelConsumedProtocol(BaseModel):
    """A protocol this ticket's work consumes from upstream."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: str = Field(
        ..., description="Protocol class name (e.g. ProtocolEventBus)."
    )
    module: str = Field(..., description="Python module path for the protocol.")
    mock_available: bool = Field(
        default=False,
        description="Whether a mock/stub exists for this protocol.",
    )


class ModelProducedProtocol(BaseModel):
    """A protocol this ticket's work produces for downstream."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    protocol: str = Field(..., description="Protocol class name.")
    module: str = Field(..., description="Python module path for the protocol.")


class ModelSeamTopics(BaseModel):
    """Kafka topics this ticket's work touches."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    subscribe: tuple[str, ...] = Field(
        default_factory=tuple, description="Topics consumed."
    )
    publish: tuple[str, ...] = Field(
        default_factory=tuple, description="Topics produced."
    )


class ModelSeamBoundaries(BaseModel):
    """Seam boundary declaration for a ticket contract.

    Declares the interfaces at the boundary of a piece of work:
    what protocols are consumed (upstream deps), what is produced
    (downstream value), and what runtime resources are touched.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    consumes: tuple[ModelConsumedProtocol, ...] = Field(
        default_factory=tuple, description="Protocols consumed from upstream."
    )
    produces: tuple[ModelProducedProtocol, ...] = Field(
        default_factory=tuple, description="Protocols produced for downstream."
    )
    topics: ModelSeamTopics = Field(
        default_factory=ModelSeamTopics, description="Kafka topics touched."
    )
    tables_read: tuple[str, ...] = Field(
        default_factory=tuple, description="DB tables read."
    )
    tables_write: tuple[str, ...] = Field(
        default_factory=tuple, description="DB tables written."
    )

    @property
    def all_consumes_mockable(self) -> bool:
        """True if every consumed protocol has a mock available."""
        return all(c.mock_available for c in self.consumes)
