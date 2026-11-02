# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Resolved contract configuration for the arm producer's effect ports."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


class ModelArmRuntimeBinding(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    workspace_root: Path
    native_timeout_s: float = Field(gt=0)
    pack_timeout_s: float = Field(gt=0)
    profile_name: str
    tenant_id: str | None
    pack_requested_topic: str
    pack_built_topic: str
    pack_failed_topic: str
    native_requested_topic: str
    native_completed_topic: str
