# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Model the settings store validates the delegation check responder row through (OMN-26043).

The row is one list: the delegation responder chain every delegation check sends its task through,
in the order the chain tries it. Local responders come first, then OpenRouter, then the Claude API.
A local responder is an ``http://<ipv4>:<port>`` base that serves ``/v1/models``; the two cloud
tiers name no address, the routing contract resolves them. No value lives here.
"""

from __future__ import annotations

import ipaddress
import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

_LOCAL_BASE = re.compile(
    r"^http://(?P<host>[0-9]{1,3}(?:\.[0-9]{1,3}){3}):(?P<port>[0-9]{1,5})$"
)

#: The tiers of the chain, in the order the chain tries them.
CHAIN_TIER_ORDER: tuple[str, ...] = ("local", "openrouter", "claude_api")


class ModelDelegationCheckResponder(BaseModel):
    """One responder of the chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: Literal["local", "openrouter", "claude_api"]
    # Required for a local responder, absent for a cloud tier.
    base_url: str | None = None

    @model_validator(mode="after")
    def _base_matches_tier(self) -> Self:
        if self.tier != "local":
            if self.base_url is not None:
                raise ValueError(f"a {self.tier} responder names no base_url")
            return self
        match = _LOCAL_BASE.match(self.base_url or "")
        if match is None:
            raise ValueError("a local responder is http://<ipv4>:<port>")
        try:
            address = ipaddress.IPv4Address(match.group("host"))
        except ipaddress.AddressValueError:
            raise ValueError("a local responder host is an IPv4 address") from None
        if not (address.is_private or address.is_loopback):
            raise ValueError(
                "a local responder is on loopback or a private address, never a public one"
            )
        if not 0 < int(match.group("port")) < 65536:
            raise ValueError("a local responder port is 1 to 65535")
        return self


class ModelDelegationCheckRespondersOverlay(BaseModel):
    """The delegation check responders overlay document."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal[1]
    responders: tuple[ModelDelegationCheckResponder, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _chain_order(self) -> Self:
        ranks = [CHAIN_TIER_ORDER.index(r.tier) for r in self.responders]
        if ranks != sorted(ranks):
            raise ValueError(
                "the chain tries local responders first, then openrouter, then claude_api"
            )
        for tier in ("openrouter", "claude_api"):
            if sum(1 for r in self.responders if r.tier == tier) > 1:
                raise ValueError(f"the chain names {tier} at most once")
        bases = [r.base_url for r in self.responders if r.tier == "local"]
        if len(bases) != len(set(bases)):
            raise ValueError("a local responder is named once")
        return self


__all__ = [
    "CHAIN_TIER_ORDER",
    "ModelDelegationCheckResponder",
    "ModelDelegationCheckRespondersOverlay",
]
