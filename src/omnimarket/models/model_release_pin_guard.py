# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Wire types of the release and pin guard (OMN-18594)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ModelReleasePinGuardRequest(BaseModel):
    """One tool call a session or lane is about to make, with the grants in force."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tool_name: str = Field(min_length=1)
    tool_input: dict[str, JsonValue] = Field(default_factory=dict)
    cwd: str = ""
    consent_rows: tuple[str, ...] = Field(
        default=(),
        description="OPERATOR-CONSENT rows of the rolling work ledger, verbatim.",
    )
    now: datetime = Field(description="Naive UTC time of the decision.")
    current_file_text: str | None = Field(
        default=None, description="The text a Write replaces; None when absent."
    )


class ModelReleasePinGuardResult(BaseModel):
    """A refusal names the ruling and the grant path; an admit names its grant."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    refuse: bool
    exit_code: Literal[0, 1]
    shape: str = ""
    repo: str = ""
    version: str = ""
    grant: str = ""
    reason: str = ""
    details: tuple[str, ...] = ()
