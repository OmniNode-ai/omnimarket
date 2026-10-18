# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed I/O for the omniclaude pin bump (OMN-20001)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict


class EnumPinBumpRefusal(StrEnum):
    """Why a bump was refused. The workflow opens no PR for any of these."""

    NOT_A_RELEASE_VERSION = "not_a_release_version"
    NO_PIN_FOUND = "no_pin_found"
    PIN_NOT_A_RELEASE_TAG = "pin_not_a_release_tag"
    PINS_DISAGREE = "pins_disagree"
    NOT_FORWARD = "not_forward"


class ModelOmniclaudePinBumpRequest(BaseModel):
    """The workflow text that holds the omniclaude pin and the released version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ci_text: str
    new_version: str


class ModelOmniclaudePinBumpResult(BaseModel):
    """The rewritten workflow text, or the reason nothing was rewritten."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    refusal: EnumPinBumpRefusal | None = None
    detail: str = ""
    current_tag: str | None = None
    new_tag: str | None = None
    updated_text: str = ""
