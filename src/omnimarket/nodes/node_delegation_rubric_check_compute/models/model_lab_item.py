# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Private lab input schema, consumed only by the measurement CLI."""

from pydantic import BaseModel, ConfigDict


class ModelLabItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    item_key: str
    task_class: str
    gate_verdict: str
    label: str
    rater_role: str
    prompt_snapshot: str
    response_snapshot: str
    rubric_version: str | None = None
