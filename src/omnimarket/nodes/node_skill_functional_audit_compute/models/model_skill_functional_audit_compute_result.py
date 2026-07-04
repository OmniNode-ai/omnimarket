# SPDX-FileCopyrightText: 2025 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ModelSkillVerdict(BaseModel):
    """Per-skill verdict from a STATIC-ONLY audit (OMN-13926).

    Verdict vocabulary:

    * ``STATIC_OK`` — static resolution passed: the skill's backing node is
      mapped on disk, has a ``contract.yaml``, its handler file(s) exist, and
      no stub markers were found. This is a resolution check, NOT
      certification — the handler never invokes the skill or its backing
      node, so a ``STATIC_OK`` skill can still fail at live invocation (see
      OMN-13834, where ``dispatch_engine`` passed this static audit while its
      live ``--dry-run`` execution actually failed).
    * ``LIVE_VERIFIED`` — reserved. Would mean a live invocation receipt
      exists proving the skill actually ran and produced correct output.
      ``node_skill_functional_audit_compute`` performs no live invocation and
      NEVER emits this value; it is documented here so the schema makes the
      static/live distinction explicit for any future live-verification node.
    * ``stub`` — a stub marker (``NotImplementedError``,
      ``node_not_implemented: true``, ``STUB``) was found in the handler or
      contract.
    * ``gap`` — missing contract, missing/unresolvable backing node, or
      unwired handler.
    * ``error`` — the audit itself could not complete for this skill.

    A per-skill verdict must never be the bare string ``ok``/``certified`` —
    that would misrepresent a static resolution pass as a functional
    certification.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(description="Skill name")
    status: str = Field(
        description=(
            "Per-skill verdict: STATIC_OK | LIVE_VERIFIED | stub | gap | "
            "error. STATIC_OK means static resolution passed (contract + "
            "handler present, no stub markers) — it is NOT a functional "
            "certification. LIVE_VERIFIED means a live invocation receipt "
            "exists proving the skill actually ran; this handler performs no "
            "live invocation and never emits LIVE_VERIFIED. The bare string "
            "'ok' must never appear here."
        )
    )
    stubs_found: list[str] = Field(
        default_factory=list,
        description="Handler or contract paths that still contain stub markers",
    )
    gaps: list[str] = Field(
        default_factory=list,
        description="Missing contracts, broken wiring, or unreachable paths",
    )


class ModelSkillFunctionalAuditComputeResult(BaseModel):
    """Top-level RUN result. ``status`` here describes whether the audit run
    itself completed, NOT whether any individual skill is certified — see
    ``ModelSkillVerdict.status`` for the per-skill STATIC_OK/LIVE_VERIFIED
    vocabulary. A run with zero discovered skills is a configuration defect
    and raises rather than producing a vacuous ``status="ok"`` result.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: str = Field(
        description=(
            "RUN-level status: ok | error. Describes whether the audit "
            "completed, not per-skill certification — see "
            "ModelSkillVerdict.status for STATIC_OK vs LIVE_VERIFIED."
        )
    )
    verdicts: list[ModelSkillVerdict] = Field(
        default_factory=list, description="Per-skill audit verdicts"
    )
    stubs_found: list[str] = Field(
        default_factory=list,
        description="Skill names with handler or contract stub markers",
    )
    gaps: list[str] = Field(
        default_factory=list,
        description="Skill names with missing contracts or broken wiring",
    )
    total_audited: int = Field(default=0, description="Total number of skills audited")
    error: str | None = Field(
        default=None, description="Error message if status is error"
    )
