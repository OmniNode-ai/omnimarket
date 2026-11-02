# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The answer of the projection query node: a status and either a JSON body or text (OMN-21288)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


class ModelProjectionQueryResponse(BaseModel):
    """Status code, media type and body of one route's answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    status_code: int
    media_type: Literal["application/json", "text/html", "text/event-stream"]
    body: Any = None
    text: str | None = None


__all__ = ["ModelProjectionQueryResponse"]
