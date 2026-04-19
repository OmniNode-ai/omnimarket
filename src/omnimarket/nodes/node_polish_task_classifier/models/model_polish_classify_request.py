# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from pydantic import BaseModel, ConfigDict


class ModelPolishClassifyRequest(BaseModel):
    """Incoming polish signal. At most one signal field should be populated."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str
    thread_body: str | None = None
    conflict_hunk: str | None = None
    ci_log: str | None = None
