# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""One watcher observation before its delivery identity is computed."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.nodes.node_pr_state_emit_effect.models.enum_pr_state import EnumPrState


class ModelPrStateEmitRequest(BaseModel):
    # RuntimeLocal validates JSON-shaped Python dictionaries. Accept wire enum
    # strings and arrays here, while keeping scalar facts strict. The event
    # subclass additionally enforces strict enum/tuple construction in Python.
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(min_length=1)
    pr_number: int = Field(gt=0, strict=True)
    state: EnumPrState
    head_sha: str
    base: str
    head_ref: str
    title: str = Field(max_length=200)
    draft: bool = Field(strict=True)
    author: str
    author_is_bot: bool = Field(strict=True)
    labels: tuple[str, ...]
    armed: bool = Field(strict=True)
    queued: bool = Field(strict=True)
    watcher_class: str
    ci_verdict: Literal["GREEN", "RED", "PENDING", "NONE"]
    red_contexts: tuple[str, ...]
    pending_contexts: tuple[str, ...]
    ci_read_at: str
    merged_at: str
    observed_at: str = Field(
        pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z$"
    )

    @field_validator("observed_at")
    @classmethod
    def validate_observed_at(cls, value: str) -> str:
        datetime.fromisoformat(value)  # Reject impossible calendar dates too.
        return value
