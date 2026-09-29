# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed source snapshot and handler result."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from omnimarket.events import delegation_eval


class ModelDelegationEventSnapshot(BaseModel):
    """Snapshot supplied by a tenant-bound delegation_events reader."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str
    response: str
    task_class: str
    gate_verdict: str
    deciding_check: str


class ModelLabelRecordResult(BaseModel):
    """Payload for the runtime-owned publishing effect."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    payload: delegation_eval.ModelDelegationEvalItemLabelled
