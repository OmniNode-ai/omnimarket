# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One rendered message of a context pack (OMN-21109)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelContextPackRenderMessage(BaseModel):
    """A {role, text} message; the rendered body is an ordered tuple of these."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    role: str
    text: str
