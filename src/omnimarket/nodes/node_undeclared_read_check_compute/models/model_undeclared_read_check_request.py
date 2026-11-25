# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The diff to judge and the overlay documents that declare settings."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelUndeclaredReadCheckRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    diff_text: str = Field(
        description="A unified diff (git diff), judged by its added lines"
    )
    overlay_documents: dict[str, Any] = Field(
        description="Overlay document name to its parsed content, every overlay of the roots the caller read"
    )


__all__ = ["ModelUndeclaredReadCheckRequest"]
