# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One delegation event, without its text, for the cell totals."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAcceptanceCellEvent(BaseModel):
    """One delegation event, without its text, for the cell totals.

    ``prompt_head`` is the start of the task text, long enough to hold a whole probe and the
    edit-loop opening line; ``prompt_chars`` is the full length.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str
    task_type: str
    prompt_head: str
    prompt_chars: int = Field(ge=0)
    terminal_ok: bool
