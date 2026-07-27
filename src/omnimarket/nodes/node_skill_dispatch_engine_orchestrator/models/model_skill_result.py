# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Skill result model — output of the dispatch_engine canonical def-B handler.

This is the terminal output of ``HandlerSkillRequested.handle`` (OMN-14806): the
skill-lifecycle boundary faithfully echoes the request identity (``skill_name`` /
``skill_path`` / ``args``) and carries the routing outcome (``status`` mirrors the
router's ``EnumDispatchEngineStatus``; ``run_id`` / ``total_selected`` /
``worker_specs`` are the real per-repo fan-out plan). A bare shim invocation with
no backlog access resolves to ``no_candidates`` — an honest empty cycle.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from .model_dispatch_engine_receipt import ModelDispatchWorkerSpec


class SkillResultStatus(StrEnum):
    """Terminal outcome of a dispatch_engine skill invocation.

    Values mirror ``EnumDispatchEngineStatus`` (the router's terminal status) so
    the skill-lifecycle boundary reports the routing outcome without lossy
    remapping, plus ``FAILED`` for a boundary/handler failure.
    """

    DISPATCHED = "dispatched"
    PLANNED = "planned"
    DRY_RUN = "dry_run"
    NO_CANDIDATES = "no_candidates"
    FAILED = "failed"


class ModelSkillResult(BaseModel):
    """Output from the dispatch_engine canonical def-B ``handle`` entrypoint."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    skill_name: str = Field(..., min_length=1, description="Requested skill name.")
    skill_path: str = Field(..., description="Path to the skill's SKILL.md file.")
    status: SkillResultStatus = Field(..., description="Terminal routing outcome.")
    args: dict[str, str] = Field(
        default_factory=dict, description="Echoed request argument pairs."
    )
    run_id: str = Field(
        default="", description="Router run id for the cycle (empty on dry_run)."
    )
    total_selected: int = Field(
        default=0, ge=0, description="Candidates that survived the router cuts."
    )
    worker_specs: tuple[ModelDispatchWorkerSpec, ...] = Field(
        default_factory=tuple, description="Per-repo worker specs the run routed to."
    )
    error: str | None = Field(
        default=None, description="Failure detail; None on success."
    )


__all__ = ["ModelSkillResult", "SkillResultStatus"]
