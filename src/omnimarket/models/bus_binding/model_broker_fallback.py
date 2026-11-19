# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A second address the lane's broker answers on, declared in the lane declaration (OMN-20540)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ModelBrokerFallback(BaseModel):
    """One ``broker_fallbacks`` entry of a lane: where to dial next, and which names to dial there.

    ``bootstrap`` is ``host:port`` of the same broker listener. ``resolve`` maps a host name the broker
    advertises in its metadata to the address to dial while the connection runs through this entry: a client
    that bootstraps through the second address is told, by the broker, to dial the first name for every
    produce and fetch, so without the mapping the second address would open and then stall on the first name.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    bootstrap: str = Field(min_length=1)
    resolve: dict[str, str] = Field(default_factory=dict)

    @field_validator("bootstrap")
    @classmethod
    def _host_and_port(cls, value: str) -> str:
        host, sep, port = value.strip().rpartition(":")
        if not sep or not host or not port.isdigit():
            raise ValueError(f"bootstrap must be host:port, got {value!r}")
        return value.strip()

    @field_validator("resolve")
    @classmethod
    def _names_and_addresses(cls, value: dict[str, str]) -> dict[str, str]:
        for name, address in value.items():
            if not name.strip() or not address.strip():
                raise ValueError("resolve maps a host name to a non-empty address")
        return {
            name.strip().lower(): address.strip() for name, address in value.items()
        }
