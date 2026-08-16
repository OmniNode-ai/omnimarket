# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""Result of one publish attempt from the emit-daemon publisher loop (OMN-15861).

The loop used to collapse a publish attempt into a bare ``bool``: ``True`` meant
"``publish_fn`` did not raise", and that bool alone drove the outbox ack. That
is the invariant-7 violation this ticket closes -- a produce call returning is
not evidence the record landed.

Splitting the attempt into *did the call complete* and *what coordinate came
back* is what lets the loop ask a separate question -- "did an authoritative
surface confirm this coordinate?" -- before truncating anything. A single bool
cannot carry that, and a bare ``ModelPublishReceipt | None`` cannot either,
because ``None`` would conflate "publish raised" with "publish succeeded on a
transport that reports no coordinate". Those two need different handling: the
first is a broker failure that should trip the circuit breaker, the second is a
durability *capability* gap that must fail closed without opening the circuit.
"""

from __future__ import annotations

from omnibase_infra.event_bus.models import ModelPublishReceipt
from pydantic import BaseModel, ConfigDict, Field


class ModelPublishAttempt(BaseModel):
    """Outcome of invoking ``publish_fn`` once.

    Attributes:
        succeeded: Whether the publish call completed without raising. This is
            explicitly NOT a durability claim -- see the module docstring.
        receipt: The durability coordinate the transport reported, when it
            reported one. ``None`` with ``succeeded=True`` means the transport
            cannot support a durable claim at all.

    Example:
        >>> ModelPublishAttempt(succeeded=False).receipt is None
        True
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    succeeded: bool = Field(..., description="publish_fn returned without raising")
    receipt: ModelPublishReceipt | None = Field(
        default=None, description="Coordinate reported by the transport, if any"
    )


__all__: list[str] = ["ModelPublishAttempt"]
