# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and result for the host install readback compute (OMN-23957).

The request carries the render text (the unit text the declared render produced) and the installed text (the text
read back from the installed file(s)), along with the host and unit identity. The result names both digests and the
comparison status: match, mismatch, or absent.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ModelHostInstallReadbackRequest(BaseModel):
    """The render text, the installed text (or absence), and the host/unit identity."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    host: str = Field(min_length=1)
    unit: str = Field(min_length=1)
    render_text: str
    installed_present: bool
    installed_text: str


class ModelHostInstallReadbackResult(BaseModel):
    """The comparison result: status, both digests, and a detail string."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID
    host: str
    unit: str
    status: Literal["match", "mismatch", "absent"]
    render_digest: str
    installed_digest: str
    detail: str


__all__: list[str] = [
    "ModelHostInstallReadbackRequest",
    "ModelHostInstallReadbackResult",
]
