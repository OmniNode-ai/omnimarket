# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Local ModelCiFixCommand definition for node_ci_fix_effect [OMN-8993].

This mirrors the definition from node_merge_sweep_triage_orchestrator PR #333 (OMN-8987).
Once OMN-8987 merges, the handler should import from:
  omnimarket.nodes.node_merge_sweep_triage_orchestrator.models.model_triage_request
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ModelCiFixCommand(BaseModel):
    """Command to diagnose and patch a failing CI job via LLM."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pr_number: int
    repo: str
    run_id_github: str
    failing_job_name: str
    correlation_id: UUID
    run_id: str
    routing_policy: dict[str, Any]
