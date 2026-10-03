# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Content-free inputs supplied by the existing receipt and hook readers."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class ModelHookReceipt(BaseModel):
    """JI.1 receipt fields used by this compute; the caller selects these fields.

    ``command_sha256`` is compared verbatim, never reconstructed. Capture hashes
    canonical JSON of the entire tool input (after scrubbing), so legacy hashes
    of the command string normally fall back to time. ``tool_input_sha256`` is
    an optional capture-compatible digest supplied by a caller that has it.
    ``answer_ref`` is carried as metadata and is never read.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt_key: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    started_at_ms: int = Field(ge=0)
    ended_at_ms: int = Field(ge=0)
    command_sha256: Sha256 | None = None
    tool_input_sha256: Sha256 | None = None
    answer_sha256: Sha256 | None = None
    answer_ref: str | None = None
    hook_session_id: str | None = Field(default=None, min_length=1)
    hook_agent_id: str | None = Field(default=None, min_length=1)
    pr: str | None = None

    @model_validator(mode="after")
    def ordered_window(self) -> ModelHookReceipt:
        if self.started_at_ms > self.ended_at_ms:
            raise ValueError("receipt start must not follow its end")
        return self


class ModelHookEvent(BaseModel):
    """Projection columns and payload digests, reduced like hook_events.py's Ev.

    Timestamps are UTC epoch milliseconds; cursor is the captured ordering key.
    A null agent is the session's main thread. The compute reads no event body.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cursor: int = Field(default=0, ge=0)
    timestamp_ms: int = Field(ge=0)
    session_id: str = Field(min_length=1)
    agent_id: str | None = Field(default=None, min_length=1)
    hook_event_name: str = Field(min_length=1)
    tool_name: str | None = None
    tool_use_id: str | None = None
    tool_input_sha256: Sha256 | None = None


class ModelHookLaneRow(BaseModel):
    """Typed lane attribution and outcome metadata from the caller's read."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp_ms: int = Field(ge=0)
    kind: Literal["CLAIM", "TERMINAL"]
    lane: str = Field(min_length=1)
    action: str | None = None
    pr: str | None = None


class ModelHookFollowupRequest(BaseModel):
    """A covered capture window, receipts and typed lane rows; no I/O handles."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipts: tuple[ModelHookReceipt, ...]
    hook_events: tuple[ModelHookEvent, ...]
    lane_rows: tuple[ModelHookLaneRow, ...]
    window_start_ms: int = Field(ge=0)
    window_end_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def ordered_window(self) -> ModelHookFollowupRequest:
        if self.window_start_ms > self.window_end_ms:
            raise ValueError("capture start must not follow its end")
        return self
