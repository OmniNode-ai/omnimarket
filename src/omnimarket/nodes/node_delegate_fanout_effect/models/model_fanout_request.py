# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Fanout request; invalid item options are reported as admission refusals."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class ModelFanoutItem(BaseModel):
    """A labeled task. Option values remain visible to the per-item precheck."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    label: str = ""
    prompt: str = ""
    task_type: str | None = None
    test_shaped: bool = False
    criteria: JsonValue = Field(default_factory=list)
    response_contract: JsonValue = None
    timeout_s: JsonValue = None


class ModelFanoutRequest(BaseModel):
    """One bounded batch, addressed to a declared delegation lane."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    items: list[ModelFanoutItem] = Field(min_length=1)
    lane: str = Field(default="dev", min_length=1)
    max_parallel: int = Field(default=3, ge=1)
    run_slug: str = "fanout"
    state_root: Path | None = None
