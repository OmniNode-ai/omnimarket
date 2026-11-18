# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request, result and vocabulary of the GPU power caps effect (OMN-24042).

A request names a lab host. The result carries one row per card: the wanted watts from the
gpu_power_caps settings, the card's limit before and after, and the action taken on it, so a
window's power-cap step has a typed reading.
"""

from __future__ import annotations

from enum import StrEnum, unique
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

# The host-side command prints one line: this prefix then the result as compact JSON.
RESULT_LINE_PREFIX: str = "GPU_POWER_CAPS_RESULT "


@unique
class EnumGpuPowerCapsBackend(StrEnum):
    """Which backend reads the cards and applies limits: chosen by the overlay."""

    IN_MEMORY = "in_memory"
    SUBPROCESS = "subprocess"
    # The node sends one lab work unit to the host over the bus and the host runs this node's command line locally.
    LAB_WORK = "lab_work"


@unique
class EnumGpuPowerCapsOutcome(StrEnum):
    OK = "ok"
    # The node refused before writing anything. See ``refusal``.
    REFUSED = "refused"
    # A limit was written but the card's read-back does not match the wanted watts.
    FAILED = "failed"


@unique
class EnumGpuPowerCapsRefusal(StrEnum):
    HOST_UNKNOWN = "host_unknown"
    CAPS_UNREADABLE = "caps_unreadable"
    GPUS_UNREADABLE = "gpus_unreadable"
    # The host has no card with the cap's name; nothing was written.
    CARD_NOT_FOUND = "card_not_found"
    # More than one card on the host has the cap's name; a blind write is refused.
    CARD_AMBIGUOUS = "card_ambiguous"
    # The wanted watts are outside the card's [min, max] limits; nothing was written.
    WATTS_OUT_OF_RANGE = "watts_out_of_range"
    # The lab work unit could not be sent, was refused, or no usable receipt came back; nothing is known to have been written.
    TRANSPORT_UNAVAILABLE = "transport_unavailable"


@unique
class EnumGpuPowerCapsCardAction(StrEnum):
    UNCHANGED = "unchanged"
    APPLIED = "applied"
    FAILED = "failed"


class ModelGpuPowerCapsRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")]
    requested_by: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^\S+$")]


class ModelGpuCard(BaseModel):
    """One card as read from the host."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    index: int = Field(ge=0)
    name: str = Field(min_length=1)
    power_limit_watts: int
    min_limit_watts: int
    max_limit_watts: int


class ModelGpuCap(BaseModel):
    """One cap from the gpu_power_caps settings: a card name and its target watts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(min_length=1)
    watts: int = Field(gt=0)


class ModelGpuPowerCapsCardResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    wanted_watts: int
    index: int | None = None
    before_watts: int | None = None
    after_watts: int | None = None
    action: EnumGpuPowerCapsCardAction | None = None
    detail: str | None = None


class ModelGpuPowerCapsResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    host: str
    outcome: EnumGpuPowerCapsOutcome
    refusal: EnumGpuPowerCapsRefusal | None = None
    detail: str | None = None
    cards: tuple[ModelGpuPowerCapsCardResult, ...] = ()


class ModelGpuSetRun(BaseModel):
    """What a backend reports for one power limit write."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int | None
    timed_out: bool = False
    # A short tail of the command's output, for the result's detail.
    output_tail: str = ""
