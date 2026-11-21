# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Models of the Fable review gate (OMN-24533): the overlay, the request, the receipt and the verdict.

Operator ruling 2026-11-14T23:50:35Z: every Fable diff review runs only on the orchestrating Mac. A
request that carries a Fable-class route key is admitted only when the host the review would run on is
one of the orchestrating hosts; an unreadable origin host and any other host are refused with a typed
reason.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumFableReviewRefusal(StrEnum):
    """Why a Fable-class review request was not admitted."""

    ORIGIN_NOT_ORCHESTRATING_MAC = "origin_not_orchestrating_mac"
    ORIGIN_UNREADABLE = "origin_unreadable"


def _no_duplicates(values: tuple[str, ...], what: str) -> None:
    seen = {v.strip().lower() for v in values}
    if len(seen) != len(values):
        raise ValueError(f"a {what} entry is declared twice")


class ModelFableReviewGateOverlay(BaseModel):
    """The gate settings, none in code: the Fable route keys, the orchestrating hosts, the host-name variable.

    ``fable_route_keys`` are the logical route keys that mark a review as Fable-class. ``orchestrating_hosts``
    are the hosts the ruling allows a Fable diff review to run on. ``host_name_env`` is the NAME of the
    environment variable that holds this machine's host identity, so no host value ever stands in this overlay.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    fable_route_keys: tuple[str, ...] = Field(min_length=1)
    orchestrating_hosts: tuple[str, ...] = Field(min_length=1)
    host_name_env: str = Field(min_length=1)

    @model_validator(mode="after")
    def _shape(self) -> ModelFableReviewGateOverlay:
        for name, values in (
            ("fable_route_keys", self.fable_route_keys),
            ("orchestrating_hosts", self.orchestrating_hosts),
        ):
            if any(not v.strip() for v in values):
                raise ValueError(f"an entry of {name} is blank")
            _no_duplicates(values, name)
        return self


class ModelFableReviewGateRequest(BaseModel):
    """The gate input: one review request and the host the review would run on.

    ``origin_host`` may be the empty string: an unreadable host identity, which the gate refuses with the
    typed reason. ``decided_at`` is the caller's time and must carry a UTC offset.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    route_keys: tuple[str, ...] = Field(min_length=1)
    origin_host: str
    decided_at: datetime

    @field_validator("decided_at")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("decided_at must carry a UTC offset")
        return value


class ModelFableReviewReceipt(BaseModel):
    """What the gate decided, with the request's identity and time carried through unchanged.

    ``fable_route_keys`` are the request's route keys that are Fable-class, in request order.
    ``matched_orchestrating_host`` is the overlay entry as written, set only when the origin host matched
    one of the orchestrating hosts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    origin_host: str
    matched_orchestrating_host: str | None
    fable_route_keys: tuple[str, ...]
    decided_at: datetime


class ModelFableReviewGateVerdict(BaseModel):
    """The gate's answer: admitted or refused, with the typed refusal and the receipt.

    ``applies`` is True when at least one request route key is Fable-class: the ruling speaks only then. A
    refusal is present exactly when ``admitted`` is False; an admission carries no refusal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    admitted: bool
    applies: bool
    refusal: EnumFableReviewRefusal | None
    receipt: ModelFableReviewReceipt

    @model_validator(mode="after")
    def _consistency(self) -> ModelFableReviewGateVerdict:
        if self.admitted:
            if self.refusal is not None:
                raise ValueError("an admitted verdict carries no refusal")
        elif self.refusal is None:
            raise ValueError("a refused verdict carries its typed refusal")
        return self
