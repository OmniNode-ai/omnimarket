# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Shared repo-health event payload models for cross-node consumption.

These models define the wire contract for the merge-sweep repo-health repair
lane (epic OMN-13316). The lane distinguishes a failure that the PR under repair
introduced (``PR_SCOPED``) from one that is pre-existing on the dev baseline
(``REPO_BASELINE``, consuming the OMN-13027 machine-readable baseline) or rooted
in an external dependency (``EXTERNAL_DEPENDENCY``), and is conservative about
ambiguity (``UNKNOWN`` — never silently ``REPO_BASELINE``).

The keystone node ``node_repo_health_classify_compute`` (OMN-13583) emits a
``ModelRepoHealthClassification`` from a ``ModelRepoHealthFailureEnvelope``. The
classification is a pure function of the envelope — no timestamps or wall-clock
fields — so the same input always yields byte-identical output (determinism).

Downstream lane nodes (repair EFFECT, reducer, orchestrator fan-out) import these
shared models from here instead of reaching into the node's private package.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class EnumFailureOrigin(StrEnum):
    """Origin bucket for a validation/CI/pre-commit failure under merge-sweep.

    - ``PR_SCOPED``: a failing path is in the PR's changed-file set — the failure
      is attributable to the branch under repair, stays in the PR fix lane.
    - ``REPO_BASELINE``: failing path(s) are not in the PR changed set and are
      known-failing on the dev baseline (OMN-13027) — pre-existing repo debt,
      routes to the repo-health repair lane.
    - ``EXTERNAL_DEPENDENCY``: the failure root is a missing/unreachable service,
      secret, or backend not owned by the repo — surfaced as external, no code
      repair task.
    - ``UNKNOWN``: not provably any of the above — surface evidence only; never
      auto-converted into a repo-health task.
    """

    PR_SCOPED = "pr_scoped"
    REPO_BASELINE = "repo_baseline"
    EXTERNAL_DEPENDENCY = "external_dependency"
    UNKNOWN = "unknown"


class ModelRepoHealthFailureEnvelope(BaseModel):
    """Inbound failure to classify by origin.

    Carries everything the deterministic classifier needs: the failing command
    and its exit code, the paths the failure implicated, the PR's changed-file
    set, the dev-baseline known-failing paths (OMN-13027; empty if unknown), and
    any external-dependency markers extracted upstream (empty if none). The model
    is a pure data envelope — no clock fields — so classification is deterministic.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    correlation_id: UUID = Field(
        ..., description="Correlation ID threading this failure through the lane."
    )
    repo: str = Field(
        ..., description="Repository slug, e.g. 'OmniNode-ai/omnimarket'."
    )
    pr_number: int | None = Field(
        default=None,
        description="GitHub PR number under repair, or None for a non-PR run.",
    )
    branch: str = Field(..., description="Branch the failure was observed on.")
    failing_command: str = Field(
        ..., description="The command whose non-zero exit produced the failure."
    )
    exit_code: int = Field(..., description="Exit code of the failing command.")
    failing_paths: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Paths the failure implicated (empty if no path is attributable).",
    )
    pr_changed_paths: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Files changed by the PR vs its merge-base.",
    )
    dev_baseline_paths: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "Paths known-failing on the dev baseline (OMN-13027); empty if the "
            "baseline is unknown."
        ),
    )
    external_markers: tuple[str, ...] = Field(
        default_factory=tuple,
        description=(
            "External-dependency tokens extracted upstream (e.g. 'connection "
            "refused', 'EHOSTUNREACH', 'missing secret'); empty if none."
        ),
    )


class ModelRepoHealthClassification(BaseModel):
    """Origin classification for a single failure envelope.

    A pure function of ``ModelRepoHealthFailureEnvelope`` — carries no timestamps
    or wall-clock fields, so identical input yields identical output. ``reason``
    is a human-readable evidence string and ``matched_paths`` names the paths that
    drove the decision (empty when no path was load-bearing).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: EnumFailureOrigin = Field(
        ..., description="Failure-origin bucket assigned to this envelope."
    )
    reason: str = Field(
        ..., description="Human-readable evidence explaining the classification."
    )
    matched_paths: tuple[str, ...] = Field(
        default_factory=tuple,
        description="Paths that drove the decision (empty if none were load-bearing).",
    )
    correlation_id: UUID = Field(
        ..., description="Correlation ID carried from the input envelope."
    )
    repo: str = Field(
        ..., description="Repository slug carried from the input envelope."
    )
    pr_number: int | None = Field(
        default=None,
        description="PR number carried from the input envelope, or None.",
    )
    failing_command: str = Field(
        ..., description="Failing command carried from the input envelope."
    )


__all__ = [
    "EnumFailureOrigin",
    "ModelRepoHealthClassification",
    "ModelRepoHealthFailureEnvelope",
]
