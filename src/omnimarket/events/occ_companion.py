# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared OCC companion request models.

These models are the cross-node seam between the OCC state read-EFFECT and the
OCC companion COMPUTE node. They live under ``omnimarket.events`` so effect
nodes do not reach into another node's private model package.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator

_GIT_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


class ModelObservedProbe(BaseModel):
    """A single machine-observed ``gh`` probe result."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    command: str = Field(..., description="The probe command that was executed.")
    stdout: str = Field(
        ..., description="Captured probe stdout (single compact JSON line)."
    )
    exit_code: int = Field(default=0, description="Probe exit code.")


class ModelOccContractState(BaseModel):
    """The up-front OCC-contract state for one cited ticket."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket_id: str = Field(..., description="The OMN-XXXX ticket this state is for.")
    exists: bool = Field(
        default=False, description="Does contracts/<ticket>.yaml already exist?"
    )
    merged: bool = Field(
        default=False,
        description="Is the existing contract already merged to the OCC default branch?",
    )
    existing_entry_ids: tuple[str, ...] = Field(
        default=(),
        description="dod_evidence item ids already present in the merged contract.",
    )
    whole_file_sha256: str = Field(
        default="",
        description="sha256 hex of the current committed contract bytes (empty if absent).",
    )
    raw_contract_text: str = Field(
        default="",
        description="The current committed contract YAML (empty if absent).",
    )


class ModelOccCompanionRequest(BaseModel):
    """All PR + OCC state the COMPUTE needs to render the companion, read up front."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    repo: str = Field(..., description="Product repo slug (owner/repo).")
    pr_number: int = Field(..., description="Product PR number.")
    pr_head_sha: str = Field(..., description="Real product PR head SHA (7-40 hex).")
    pr_title: str = Field(default="", description="Product PR title.")
    pr_body: str = Field(default="", description="Product PR body.")
    pr_state: str = Field(default="open", description="Product PR state.")
    pr_head_ref: str = Field(default="", description="Product PR head branch ref.")

    runner: str = Field(
        default="node_occ_companion_compute", description="Receipt runner identity."
    )
    verifier: str = Field(
        default="occ-evidence-source-autobind",
        description="Receipt verifier identity (must differ from runner).",
    )

    run_timestamp: str = Field(
        ..., description="Injected ISO-8601 authoring timestamp (no datetime.now)."
    )
    product_probe: ModelObservedProbe = Field(
        ..., description="Observed gh probe of the product PR."
    )

    occ_repo: str = Field(
        default="OmniNode-ai/onex_change_control", description="OCC repo slug."
    )
    occ_contract_states: tuple[ModelOccContractState, ...] = Field(
        default=(), description="Per-cited-ticket OCC contract state."
    )

    occ_pr_number: int | None = Field(
        default=None, description="OCC companion PR number (once opened)."
    )
    occ_head_sha: str | None = Field(
        default=None, description="OCC companion branch head SHA (once pushed)."
    )
    occ_probe: ModelObservedProbe | None = Field(
        default=None, description="Observed gh probe of the OCC PR (once opened)."
    )

    changed_files: tuple[str, ...] = Field(
        default=(), description="Product PR changed file paths."
    )
    diff_total_lines: int = Field(
        default=0, description="Total changed diff lines on the product PR."
    )

    downstream_check_value: str | None = Field(
        default=None,
        description="Content-read check_value asserting a PR-added symbol, or "
        "None to fall back to the generic PR-state check.",
    )

    @field_validator("pr_head_sha", "occ_head_sha")
    @classmethod
    def _validate_git_sha(cls, value: str | None) -> str | None:
        if value is None:
            return value
        if not _GIT_SHA_RE.fullmatch(value):
            raise ValueError("commit SHA must be 7-40 hexadecimal characters")
        return value

    @field_validator("occ_contract_states")
    @classmethod
    def _validate_unique_occ_contract_states(
        cls, value: tuple[ModelOccContractState, ...]
    ) -> tuple[ModelOccContractState, ...]:
        seen: set[str] = set()
        duplicates: list[str] = []
        for state in value:
            if state.ticket_id in seen:
                duplicates.append(state.ticket_id)
            seen.add(state.ticket_id)
        if duplicates:
            duplicate_list = ", ".join(sorted(set(duplicates)))
            raise ValueError(
                f"occ_contract_states must not contain duplicate ticket_id values: {duplicate_list}"
            )
        return value


__all__ = [
    "ModelObservedProbe",
    "ModelOccCompanionRequest",
    "ModelOccContractState",
]
