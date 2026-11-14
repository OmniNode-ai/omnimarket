# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One typed event per change of a token mark, and the settings that name its topic (OMN-19998).

Every operation of the Claude token pool that changes a mark — a limit set, replaced or cleared,
a revocation set or cleared — leaves one :class:`ModelPoolMarkEvent` on the bus, so a reader can
follow a pool's marks without reading its state file. No model here holds a token or a
fingerprint: an event names the lane or principal that asked (``who``), the host and the label,
what happened to the mark (``change``), why it happened (``cause``) and nothing else.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: The pool operations that can change a mark. ``resolve`` and the draw-only
#: operations never touch one, so they name no event.
PoolMarkVerb = Literal[
    "mark_limited",
    "mark_revoked",
    "sync_marks",
    "clear_revokes",
    "apply_limit_rechecks",
    "apply_revoke_rechecks",
    "add",
]

#: What a change did to one mark. A limit is set (``limited``), replaced by a
#: newer reset (``limit_replaced``) or cleared (``limit_cleared``); a
#: revocation is set (``revoked``, or ``challenged`` when the mark names a 403
#: challenge page), confirmed (a challenge upgraded to a real revoke while its
#: stamp stays: ``revoke_confirmed``) or cleared (``revoke_cleared``).
PoolMarkChange = Literal[
    "limited",
    "limit_replaced",
    "limit_cleared",
    "revoked",
    "challenged",
    "revoke_cleared",
    "revoke_confirmed",
]


class ModelPoolMarkEvent(BaseModel):
    """One change of one label's mark on one host, as the operation that asked for it left it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: The lane or principal that asked for the operation. The overlay's ``actor_env`` environment
    #: variable names where a caller reads this from when one of its commands runs.
    who: str = Field(min_length=1)
    #: When the operation ran. A naive value is refused; an offset is normalised to UTC.
    at: datetime
    verb: PoolMarkVerb
    host: str
    label: str
    change: PoolMarkChange
    #: Why the mark changed, in the caller's own words — a notice it read, the rule that decided,
    #: or the command that ran. Never blank.
    cause: str = Field(min_length=1)

    @field_validator("at")
    @classmethod
    def _at_is_an_aware_instant(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("at names no time zone")
        return value.astimezone(UTC)


class ModelPoolMarkDiffRequest(BaseModel):
    """One pool operation's marks before and after, as live marks per label: ``until``, ``revoked_at`` and ``revoke_cause``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    before: dict[str, dict[str, str | None]]
    after: dict[str, dict[str, str | None]]
    who: str = Field(min_length=1)
    at: datetime
    verb: PoolMarkVerb
    host: str
    #: Why the marks changed, the same for every event this operation leaves.
    cause: str = Field(min_length=1)


class ModelPoolMarkDiffResult(BaseModel):
    """The events one operation left: one per changed mark, none when nothing changed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    events: tuple[ModelPoolMarkEvent, ...] = ()


class ModelPoolMarkEventsSettings(BaseModel):
    """Where mark events go, as the overlay carries them: the topic and the ``who`` variable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str = Field(min_length=1)
    #: The environment variable that names who runs the command, so every caller of the pool
    #: resolves ``who`` for its events the same way. The sinks read only ``topic``; this is the
    #: lane's own concern.
    actor_env: str = Field(min_length=1)


__all__ = [
    "ModelPoolMarkDiffRequest",
    "ModelPoolMarkDiffResult",
    "ModelPoolMarkEvent",
    "ModelPoolMarkEventsSettings",
    "PoolMarkChange",
    "PoolMarkVerb",
]
