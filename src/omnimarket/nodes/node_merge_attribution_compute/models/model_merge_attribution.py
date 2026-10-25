# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Supplied evidence for one historical merge; this node performs no reads."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

_CONFIG = ConfigDict(frozen=True, extra="forbid")


class ModelAttributionLedgerRow(BaseModel):
    model_config = _CONFIG
    ts: str
    kind: str
    cells: dict[str, str]
    text: str = ""


class ModelAttributionWorker(BaseModel):
    model_config = _CONFIG
    lease_id: int
    subject: str
    fix_ref: str | None = None
    released_at: str
    engine: str
    host: str
    result: str


class ModelMergeAttributionRequest(BaseModel):
    model_config = _CONFIG
    pr: str
    merged_at: str
    host_identity_prefix: str | None = Field(
        default=None,
        min_length=1,
        description="Caller-supplied prefix of per-host commit account names.",
    )
    author: str = ""
    committer: str = ""
    message: str = ""
    title: str = ""
    rows: tuple[ModelAttributionLedgerRow, ...] = ()
    workers: tuple[ModelAttributionWorker, ...] = ()


class ModelMergeAttributionResult(BaseModel):
    model_config = _CONFIG
    engine: str | None = None
    host: str | None = None
    lane: str | None = None
    reason: Literal[
        "no_terminal", "missing_model", "landing_worker", "automation", "person"
    ]
    basis: str
    evidence: tuple[str, ...] = Field(default=())
