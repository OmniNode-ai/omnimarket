# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a publish run did to each host, and why it failed when it did."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from .model_prrp_snapshot_host_outcome import ModelPrrpSnapshotHostOutcome


class ModelPrrpSnapshotPublishResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exit_code: int
    captured_at: str | None = None
    repos: int = 0
    hosts: tuple[ModelPrrpSnapshotHostOutcome, ...] = ()
    failures: tuple[str, ...] = ()
