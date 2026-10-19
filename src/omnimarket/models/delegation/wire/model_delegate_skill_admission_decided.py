# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Admission decision for one client call (plan TB.3, OMN-21098).

Published on ``onex.evt.omnimarket.delegate-skill-admission-decided.v1`` after
the admission commit and before any dispatch. It is an evt topic and carries no
prompt text: the only free text is the admission ``reason``, a short code.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from omnimarket.models.claude_code_binding import EnumClientMaxMode

_SHA256_HEX = r"^[0-9a-f]{64}$"


class ModelDelegateSkillAdmissionDecided(BaseModel):
    """What admission decided for one client call, and under which mode."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(..., description="The command's correlation id.")
    client_call_key: str = Field(
        ..., pattern=_SHA256_HEX, description="Identity of the client call."
    )
    request_fingerprint: str = Field(
        ..., pattern=_SHA256_HEX, description="Fingerprint of the canonical request."
    )
    admitted: bool = Field(..., description="Whether the call may be dispatched.")
    task_type: str | None = Field(
        default=None, description="Task class admission resolved, if any."
    )
    reason: str = Field(..., min_length=1, description="Short admission reason code.")
    effective_mode: EnumClientMaxMode = Field(
        ...,
        description="min(client_max_mode, server mode for the client host).",
    )


__all__: list[str] = ["ModelDelegateSkillAdmissionDecided"]
