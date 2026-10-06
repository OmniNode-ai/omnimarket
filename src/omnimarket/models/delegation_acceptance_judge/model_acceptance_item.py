# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One delegated task and its output, as the judge will see them."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelAcceptanceItem(BaseModel):
    """One delegated task and its output, as the judge will see them.

    ``item_id`` must be opaque to the judge: it is shown, the model name never is.
    ``kind`` empty means the rubric derives it from the task text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    item_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    task_type: str = Field(min_length=1)
    kind: str = ""
    task_text: str
    answer_text: str
    note: str = ""
