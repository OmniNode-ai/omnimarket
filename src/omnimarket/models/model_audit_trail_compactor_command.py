# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared audit trail compactor command model.

The compactor node consumes this command and the schedule node produces it, so it
lives in omnimarket.models and in neither node's models package (OMN-9263).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCompactorCommand(BaseModel):
    """Input command for the audit trail compactor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    friction_dir: str = Field(
        default=".onex_state/friction",
        description="Path to friction event directory.",
    )
    dispatch_log_path: str = Field(
        default=".onex_state/dispatch-log.ndjson",
        description="Path to dispatch log NDJSON file.",
    )
    lookback_days: int = Field(
        default=7,
        description="Number of days to look back for the rollup.",
    )
    dry_run: bool = Field(default=False, description="Skip side effects when true.")
