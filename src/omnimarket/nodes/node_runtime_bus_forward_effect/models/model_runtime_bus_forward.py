# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the runtime bus forward node (OMN-24003): the recorded events, the command, and the typed result."""

from __future__ import annotations

import base64

from pydantic import BaseModel, ConfigDict, Field


class ModelForwardEvent(BaseModel):
    """One event a run published to the runtime-local bus, as the recorder handed it over.

    The value (and the key, when the event carried one) is base64, so a recorded event crosses into this node without an
    encoding guess; ``value_bytes`` and ``key_bytes`` are the decode.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1)
    key_b64: str | None = None
    value_b64: str
    headers: dict[str, str] = Field(default_factory=dict)

    def value_bytes(self) -> bytes:
        """The event's value, decoded."""
        return base64.b64decode(self.value_b64)

    def key_bytes(self) -> bytes | None:
        """The event's key, decoded; ``None`` when the event carried no key."""
        return None if self.key_b64 is None else base64.b64decode(self.key_b64)


class ModelRuntimeBusForwardCommand(BaseModel):
    """The run whose recorded runtime-bus events are carried to the hosted bus."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    run_id: str = Field(min_length=1)
    events: tuple[ModelForwardEvent, ...] = ()


class ModelForwardRefusal(BaseModel):
    """One recorded event this node did not publish, and why."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    reason: str


class ModelRuntimeBusForwardResult(BaseModel):
    """What a run's recorded events came to on the hosted bus.

    ``forwarded`` counts the events handed to the publisher; ``refusals`` carries one entry per event never handed over
    (a topic under none of the overlay's allowed prefixes, an event past the overlay's ``max_events``), by name and reason;
    ``failed`` counts every event that did not land — the refusals included — so ``ok`` reads true exactly when the whole
    recording was carried.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    forwarded: int = Field(ge=0)
    failed: int = Field(ge=0)
    refusals: tuple[ModelForwardRefusal, ...] = ()
    detail: str | None = None

    @property
    def ok(self) -> bool:
        """True when no recorded event failed to reach the hosted bus."""
        return self.failed == 0

    def receipt(self) -> dict[str, object]:
        """The run's forward counts in the shape omnibase_core's ModelRuntimeBusForwardReceipt validates."""
        return {
            "forwarded": self.forwarded,
            "failed": self.failed,
            "detail": self.detail,
        }
