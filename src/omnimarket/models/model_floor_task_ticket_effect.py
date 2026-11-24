# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the floor-task ticket effect: the mint and the entry-ticket check."""

from __future__ import annotations

from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

OVERLAY_PREFIX = "floor_fill."
OVERLAY_KEYS = ("mint_timeout_seconds",)


def _refuse_missing_overlay_keys(data: Any) -> Any:
    if isinstance(data, dict):
        missing = [key for key in OVERLAY_KEYS if data.get(key) is None]
        if missing:
            raise ValueError(
                "no overlay supplies "
                + ", ".join(f"{OVERLAY_PREFIX}{k}" for k in missing)
            )
    return data


class ModelFloorTaskTicketMintRequest(BaseModel):
    """The payload of one floor task's ticket, and the deadline each call of the ticket store runs under."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    floor_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str = Field(min_length=1)
    parent: str = Field(pattern=r"^OMN-[0-9]+$")
    # floor_fill.mint_timeout_seconds: no default, a request that lacks it is refused naming the key.
    mint_timeout_seconds: float = Field(gt=0)

    @model_validator(mode="before")
    @classmethod
    def _keys_are_supplied(cls, data: Any) -> Any:
        return _refuse_missing_overlay_keys(data)


class ModelFloorTaskTicketMintResult(BaseModel):
    """The mint for one key: a usable ticket, a refusal a person fixes, or no answer this tick.

    ``cause`` of a refusal reads ``ticket-refused:<cause>`` (``old-fill-visible``, or the gate's codes); of an
    unavailable mint it names the call that failed or ran past the deadline (``search-timeout``,
    ``create-failed:<cause>``, ``readback-unreadable``, ``readback-unusable:<why>``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    state: Literal["ticket", "refused", "unavailable"]
    ticket: str | None = None
    cause: str | None = None
    # True when the ticket was found by its Floor key line and nothing was created.
    reused: bool = False

    @model_validator(mode="after")
    def _shape_follows_state(self) -> Self:
        if self.state == "ticket":
            if not self.ticket or self.cause is not None:
                raise ValueError("a ticket result names the ticket and no cause")
        elif self.ticket is not None or not self.cause or self.reused:
            raise ValueError(f"{self.state} names a cause and no ticket")
        return self


class ModelFloorTaskEntryTicketRequest(BaseModel):
    """One entry ticket to read: the task it was stored for and the deadline of the read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    ticket: str = Field(min_length=1)
    mint_timeout_seconds: float = Field(gt=0)

    @model_validator(mode="before")
    @classmethod
    def _keys_are_supplied(cls, data: Any) -> Any:
        return _refuse_missing_overlay_keys(data)


class ModelFloorTaskEntryTicketVerdict(BaseModel):
    """What the ticket store says about the entry ticket of one task."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    line_id: str
    task_id: str
    status: Literal["usable", "refused", "unreadable"]
    ticket: str = ""
    refusal: Literal["canceled", "done", "umbrella", "old-fill-visible"] | None = None
    cause: str = ""

    @model_validator(mode="after")
    def _status_carries_what_it_needs(self) -> Self:
        if self.status == "usable" and not self.ticket:
            raise ValueError("a usable entry ticket names the ticket")
        if self.status == "refused" and self.refusal is None:
            raise ValueError("a refused entry ticket names its refusal")
        if self.status != "refused" and self.refusal is not None:
            raise ValueError("only a refused entry ticket carries a refusal")
        return self
