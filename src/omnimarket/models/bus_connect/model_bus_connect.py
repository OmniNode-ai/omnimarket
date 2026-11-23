# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a bus client waits for, and where it connects second (OMN-26027)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ModelBrokerFallbackDeclaration(BaseModel):
    """A declared second address, before it is resolved to an address (policy or lane entry)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["hook_edge_lane"] = Field(
        description="Where the address is declared: the lane's entry in hook_edge_lane.yaml"
    )
    advertised_names: tuple[str, ...] = Field(
        default=(),
        description="Host names the broker advertises in its metadata, dialled at the address's host",
    )
    advertised_names_from_bootstrap: bool = Field(
        default=False,
        description="The broker advertises the host the first address names, so dial that host here",
    )


class ModelBusConnectPolicy(BaseModel):
    """The bounds one bus connect runs under. Both values are declared, never typed in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    connect_deadline_seconds: float = Field(
        gt=0, description="Wait for one broker address to start before giving up on it"
    )
    abandon_grace_seconds: float = Field(
        gt=0,
        description="Wait for a cancelled start to wind down before it is abandoned",
    )
    default_broker_fallbacks: tuple[ModelBrokerFallbackDeclaration, ...] = Field(
        default=(),
        description="Second addresses used for a lane that declares no broker_fallbacks of its own",
    )


class ModelBrokerFallback(BaseModel):
    """A second address of the lane's broker, read from the lane declaration."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    address: str = Field(description="host:port the fallback connects to")
    advertised_names: tuple[str, ...] = Field(
        default=(),
        description="Host names the broker advertises in its metadata, dialled at the address's host",
    )
    advertised_names_from_bootstrap: bool = Field(
        default=False,
        description="Also dial the host of the first address at this address's host",
    )


class ModelBusConnectAttempt(BaseModel):
    """One address a connect tried and how it ended."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    address: str
    timed_out: bool
    reason: str
