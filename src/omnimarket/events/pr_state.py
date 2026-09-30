# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared PR-state event vocabulary (OMN-19999).

The PR-state emit node produces these models and the PR-state projection node
consumes them, so they live in the shared ``omnimarket.events`` package rather
than in either node.

Canonical JSON for the observed event uses sorted keys, compact separators,
UTF-8 (unescaped Unicode), no NaN, and the declared tuple order. Only
observed_at and digest are excluded. A supplied digest is checked, so corrupted
wire payloads cannot claim an identity.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EnumPrState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


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


class ModelPrStateObservedEvent(ModelPrStateEmitRequest):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    digest: str = Field(default="", pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_digest(self) -> Self:
        canonical = json.dumps(
            self.model_dump(mode="json", exclude={"observed_at", "digest"}),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if self.digest and self.digest != digest:
            raise ValueError("digest does not match canonical PR state")
        object.__setattr__(self, "digest", digest)
        return self
