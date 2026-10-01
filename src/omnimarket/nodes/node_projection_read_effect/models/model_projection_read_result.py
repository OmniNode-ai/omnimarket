# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The answer to a projection read: a page, or a named refusal (OMN-20159)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ModelProjectionReadResult(BaseModel):
    """One read's outcome.

    ``response`` is the body the standalone API's ``GET /projection/{topic}``
    returns for the same request and ``http_status`` its status code, so a
    client moving from that route to this node reads the same document. The
    typed fields lift what a caller branches on. A refusal always names its
    ``error``; ``ok`` is never true with an error, and never false without one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    topic: str
    ok: bool
    error: str | None = None
    detail: str | None = None
    http_status: int = Field(ge=100, le=599)
    row_count: int = Field(default=0, ge=0)
    rows: list[dict[str, Any]] = Field(default_factory=list)
    tenant: str | None = None
    next_cursor: str | None = None
    truncated: bool = False
    response: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _ok_iff_no_error(self) -> ModelProjectionReadResult:
        if self.ok == (self.error is not None):
            raise ValueError("a read is ok exactly when it names no error")
        if not self.ok and self.rows:
            raise ValueError("a refused read carries no rows")
        return self


__all__ = ["ModelProjectionReadResult"]
