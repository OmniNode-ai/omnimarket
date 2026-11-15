# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The real-backend profile of the panel row reader: where the row is read from, named by the overlay (OMN-19998)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelWorkStatePanelRowReaderRoute(BaseModel):
    """The environment variable holding the DSN, the table and the read timeout; no default, no literal in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dsn_env: str = Field(min_length=1)
    table: str = Field(pattern=r"^[a-z_][a-z0-9_]*$")
    timeout_s: int = Field(gt=0)


__all__ = ["ModelWorkStatePanelRowReaderRoute"]
