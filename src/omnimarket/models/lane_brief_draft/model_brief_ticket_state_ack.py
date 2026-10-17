# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A ticket-state acknowledgement the shell reads from the live ticket."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelBriefTicketStateAck(BaseModel):
    """Rendered as `ticket-state-ack=<ticket>:<state>`, the form the brief checker reads."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: str = Field(pattern=r"^OMN-[0-9]+$")
    state: str = Field(pattern=r"^[A-Za-z]+$")
