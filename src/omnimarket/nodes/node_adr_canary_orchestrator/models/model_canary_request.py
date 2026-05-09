# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelCanaryCommandPayload -- input contract for node_adr_canary_orchestrator.

[OMN-10698]
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelCanaryCommandPayload(BaseModel):
    """Command payload for the ADR canary evaluation pipeline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_path: str = Field(
        default="docs/adr-canary/ground_truth_manifest.yaml",
        description=(
            "Path to the ground truth manifest YAML, relative to the repo root "
            "or absolute. All manifest entries are evaluated unless model_subset filters them."
        ),
    )
    model_subset: list[str] | None = Field(
        default=None,
        description=(
            "Restrict evaluation to this list of model keys. None = evaluate all models "
            "declared in the manifest."
        ),
    )
    output_dir: str = Field(
        default="docs/adr-canary-runs/",
        description="Base directory for canary run evidence bundles.",
    )
    dry_run: bool = Field(
        default=False,
        description="Log what would be evaluated without making LLM calls.",
    )
    resume_run_id: str | None = Field(
        default=None,
        description=(
            "Resume an interrupted run by providing its run_id. The orchestrator will "
            "skip manifest entries that already have a completed evidence bundle."
        ),
    )
    max_cost_usd: float | None = Field(
        default=None,
        description="Hard budget cap in USD. None = no cap.",
        ge=0.0,
    )
    allow_external_providers: bool = Field(
        default=False,
        description="Allow LLM calls to external (non-local) providers.",
    )


__all__: list[str] = ["ModelCanaryCommandPayload"]
