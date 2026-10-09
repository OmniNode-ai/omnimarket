# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Required contract configuration; no threshold defaults."""

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class ModelLedgerSource(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    relation: str = Field(pattern=r"^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$")
    binding_ref: str = Field(min_length=1)
    dsn_env: str = Field(min_length=1)


class ModelParityWitness(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    ledger_path_env: str = Field(min_length=1)
    archive_dir_name: str = Field(pattern=r"^[a-z_][a-z0-9_-]*$")


class ModelBranchClaimPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    repositories: tuple[str, ...] = Field(min_length=1)
    check_name: str = Field(min_length=1)
    outcome_marker_prefix: str = Field(min_length=1)
    read_window_hours: int = Field(gt=0, strict=True)
    staleness_hours: int = Field(gt=0, strict=True)
    max_pr_commits: int = Field(gt=0, strict=True)
    claim_transition_rows: tuple[str, ...] = Field(min_length=1)
    ledger_source: ModelLedgerSource
    parity_witness: ModelParityWitness
    max_lane_length: int = Field(gt=0, strict=True)
    short_sha_length: int = Field(gt=0, strict=True)
    max_finding_chars: int = Field(gt=0, strict=True)
    max_title_chars: int = Field(gt=0, strict=True)
    commits_per_page: int = Field(gt=0, strict=True)


class BranchClaimPolicyError(ValueError):
    """The node cannot use the contract as declared."""


def load_branch_claim_policy(contract_path: Path) -> ModelBranchClaimPolicy:
    try:
        raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or not isinstance(raw.get("branch_claim"), dict):
            raise ValueError("branch_claim must be a mapping")
        return ModelBranchClaimPolicy.model_validate(raw["branch_claim"])
    except (OSError, ValueError, yaml.YAMLError) as exc:
        raise BranchClaimPolicyError(
            f"branch claim contract unreadable: {exc}"
        ) from exc
