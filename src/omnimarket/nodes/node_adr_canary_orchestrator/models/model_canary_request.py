# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT

"""ModelCanaryCommandPayload -- input contract for node_adr_canary_orchestrator.

[OMN-10698]
"""

from __future__ import annotations

from pathlib import PurePosixPath

from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnimarket.models.adr import (
    EnumAdrKBDestination,
    ModelAdrSourceProvenance,
)


class ModelCanaryCommandPayload(BaseModel):
    """Command payload for the ADR canary evaluation pipeline."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    manifest_path: str = Field(
        default="src/omnimarket/configs/adr_canary_ground_truth_manifest.v1.yaml",
        description=(
            "Path to the ground truth manifest YAML, relative to the repo root "
            "or absolute. All manifest entries are evaluated unless model_subset filters them."
        ),
    )
    workspace_root: str | None = Field(
        default=None,
        description=(
            "Canonical OMNI_HOME workspace root used to resolve relative discovery "
            "manifest paths. When omitted, the handler requires OMNI_HOME."
        ),
    )
    model_subset: list[str] | None = Field(
        default=None,
        description=(
            "Restrict evaluation to this list of model keys. None = evaluate all models "
            "declared in the manifest."
        ),
    )
    scoped_files: tuple[str, ...] | None = Field(
        default=None,
        description=(
            "Optional workspace-relative source files. The orchestrator filters "
            "manifest entries to this scope without accepting ad-hoc payload keys."
        ),
    )
    source_provenance: ModelAdrSourceProvenance | None = Field(
        default=None,
        description=(
            "Explicit source owner classification for scoped invocation. When "
            "provided it must match every selected manifest entry."
        ),
    )
    kb_destination: EnumAdrKBDestination | None = Field(
        default=None,
        description=(
            "Explicit closed KB destination for scoped invocation. When provided "
            "it must match every selected manifest entry."
        ),
    )
    output_dir: str = Field(
        default=".onex_state/adr-canary-runs/",
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
        description=(
            "Hard budget cap in USD. None = no cap. A supplied cap currently "
            "fails closed until extraction has a prospective token bound and "
            "a matching cost_pricing route."
        ),
        ge=0.0,
    )
    allow_external_providers: bool = Field(
        default=False,
        description="Allow LLM calls to external (non-local) providers.",
    )

    @field_validator("scoped_files")
    @classmethod
    def _require_workspace_relative_scoped_files(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if value is None:
            return value
        if not value:
            raise ValueError("scoped_files must not be empty when supplied")
        for source_path in value:
            path = PurePosixPath(source_path)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("scoped_files must contain workspace-relative paths")
        return value


__all__: list[str] = ["ModelCanaryCommandPayload"]
