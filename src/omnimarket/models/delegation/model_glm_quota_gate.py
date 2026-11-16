# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the GLM Coding Plan quota gate the lane delegation step reads (OMN-24651)."""

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict


class ModelGlmQuotaGateRequest(BaseModel):
    """One question to the gate: ``check`` (is GLM held out now) or ``note`` (read a failed call's error, then answer)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    operation: Literal["check", "note"]
    now: AwareDatetime
    error_text: str = ""
    marker_until: AwareDatetime | None = (
        None  # the hold the state marker already carries, when it holds one
    )


class ModelGlmQuotaGateResult(BaseModel):
    """Whether GLM is held out, until when, the text a skipped call's receipt carries, and the hold to persist."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    held: bool
    until: AwareDatetime | None = None
    reason: str = ""
    write_until: AwareDatetime | None = (
        None  # a hold the caller persists; None leaves the marker as it is
    )
