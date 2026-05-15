# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request model for node_dependency_health_sweep."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ModelDepHealthSweepRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    repo_roots: list[str]
    scope: list[str] | None = None
    severity_threshold: str = "MAJOR"
    dry_run: bool = False
    baseline_path: str | None = None
    run_id: str | None = None
