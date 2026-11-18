# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the lane-brief context pack: seven tiers, the rungs each model-selected tier tried, and a hash."""

from __future__ import annotations

import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.enums.enum_lane_brief_tier import (
    MODEL_SELECTED_TIERS,
    EnumLaneBriefRung,
    EnumLaneBriefTier,
    EnumLaneBriefTierMode,
)

_CLAUDE_IDENTITY = re.compile(r"(?:anthropic|claude|haiku|opus|sonnet)", re.IGNORECASE)


def is_claude_assembler(rung: EnumLaneBriefRung | None, model: str | None) -> bool:
    return rung is EnumLaneBriefRung.SONNET or bool(
        model and _CLAUDE_IDENTITY.search(model)
    )


class ModelLaneBriefJudgement(BaseModel):
    """Provenance associated with a delegation answer."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rung: EnumLaneBriefRung | None = None
    model: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:/-]+$")
    run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_.:-]+$")


class ModelLaneBriefSummary(ModelLaneBriefJudgement):
    text: str = Field(min_length=1)


class ModelLaneBriefReviewComment(BaseModel):
    """One review comment the brief names, with its whole posted text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    comment_id: str = Field(min_length=1)
    text: str = Field(min_length=1)


class ModelLaneBriefExemplarCandidate(BaseModel):
    """A file the model-selected exemplar tier may choose, with its text."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    text: str


class ModelLaneBriefExemplarChoice(ModelLaneBriefJudgement):
    """What a rung chose: the path of the closest example and the shape excerpt cut from it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)

    @field_validator("excerpt")
    @classmethod
    def _excerpt_is_not_blank(cls, excerpt: str) -> str:
        if not excerpt.strip():
            raise ValueError(
                "excerpt is blank; a choice carries text cut from the file"
            )
        return excerpt


class ModelLaneBriefContextRequest(BaseModel):
    """What the lane will do and the facts the deterministic tiers are read from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    task: str = Field(min_length=1)
    profile_name: str = Field(default="lane_brief_context", min_length=1)
    # The repository the paths are in: a ledger row may name a path as ``<repository>/src/a.py``.
    repository: str = Field(
        default="omnimarket", pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?$"
    )
    pull_request: str | None = Field(
        default=None, pattern=r"^[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)?#[1-9][0-9]*$"
    )
    revision: str | None = Field(default=None, pattern=r"^[0-9a-f]{40}$")
    change_kind: str = Field(default="design-build", min_length=1)
    node_archetype: str | None = Field(default=None, min_length=1)
    ticket_criteria: tuple[str, ...] = ()
    required_ci_contexts: tuple[str, ...] = ()
    files_to_touch: tuple[str, ...] = ()
    review_comments: tuple[ModelLaneBriefReviewComment, ...] = ()
    exemplar_candidates: tuple[ModelLaneBriefExemplarCandidate, ...] = ()
    ledger_rows: tuple[str, ...] | None = None

    @field_validator("files_to_touch")
    @classmethod
    def _no_blank_path(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        if any(not p.strip() for p in paths):
            raise ValueError("files_to_touch names a blank path; every path is a file")
        return paths

    @field_validator("exemplar_candidates")
    @classmethod
    def _no_duplicate_candidate_path(
        cls, candidates: tuple[ModelLaneBriefExemplarCandidate, ...]
    ) -> tuple[ModelLaneBriefExemplarCandidate, ...]:
        paths = [c.path for c in candidates]
        if repeated := sorted({p for p in paths if paths.count(p) > 1}):
            raise ValueError(
                f"exemplar_candidates names a path twice: {repeated}; one path is one candidate"
            )
        return candidates


class ModelLaneBriefRungAttempt(ModelLaneBriefJudgement):
    """One rung a model-selected tier tried and what came of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rung: EnumLaneBriefRung
    outcome: Literal["selected", "fell_back"]
    reason: str | None = None

    @model_validator(mode="after")
    def _a_fallback_says_why(self) -> ModelLaneBriefRungAttempt:
        if is_claude_assembler(self.rung, self.model):
            raise ValueError("Claude cannot assemble a lane-brief judgment")
        if self.outcome == "fell_back" and not (self.reason or "").strip():
            raise ValueError("a rung that fell back records why")
        if self.outcome == "selected" and not (self.model and self.run_id):
            raise ValueError("a selected rung records its model and run id")
        return self


class ModelLaneBriefTierRecord(BaseModel):
    """How one tier was read. A deterministic tier records that no model was called."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    tier: EnumLaneBriefTier
    mode: EnumLaneBriefTierMode
    model_called: bool
    rungs: tuple[ModelLaneBriefRungAttempt, ...] = ()
    token_estimate: int = Field(ge=0)
    token_budget: int = Field(gt=0)

    status: Literal["assembled", "empty", "truncated", "unassembled"] = "assembled"
    reason: str | None = None
    judgement_reason: str | None = None

    @model_validator(mode="after")
    def _mode_agrees_with_calls(self) -> ModelLaneBriefTierRecord:
        if (self.mode is EnumLaneBriefTierMode.MODEL_SELECTED) != (
            self.tier in MODEL_SELECTED_TIERS
        ):
            raise ValueError(f"mode does not match tier {self.tier.value}")
        if self.model_called != bool(self.rungs):
            raise ValueError("model_called must match the recorded attempts")
        if self.mode is EnumLaneBriefTierMode.DETERMINISTIC and self.rungs:
            raise ValueError(f"deterministic tier {self.tier.value} called a model")
        if any(a.outcome == "selected" for a in self.rungs[:-1]):
            raise ValueError(
                f"tier {self.tier.value} kept walking after a rung selected"
            )
        if self.mode is EnumLaneBriefTierMode.MODEL_SELECTED:
            selected = bool(self.rungs and self.rungs[-1].outcome == "selected")
            if selected == bool(self.judgement_reason):
                raise ValueError(
                    f"tier {self.tier.value} records a selected rung or a judgement reason"
                )
        if self.status == "unassembled" and not self.reason:
            raise ValueError(f"unassembled tier {self.tier.value} records its reason")
        return self


class ModelLaneBriefContextPack(BaseModel):
    """The built pack. The hash is required and is the sha256 of ``body``, so the approver can recompute it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    profile_name: str = Field(min_length=1)
    profile_version: str = Field(min_length=1)
    profile_content_hash: str = Field(min_length=1)
    body: str = Field(min_length=1)
    final_pack_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    tiers: tuple[ModelLaneBriefTierRecord, ...]

    @model_validator(mode="after")
    def _hash_is_the_body_hash_and_every_tier_is_present(
        self,
    ) -> ModelLaneBriefContextPack:
        if self.final_pack_hash != hashlib.sha256(self.body.encode()).hexdigest():
            raise ValueError("final_pack_hash is not the sha256 of the pack body")
        if [t.tier for t in self.tiers] != list(EnumLaneBriefTier):
            raise ValueError("the pack carries every lane-brief tier once, in order")
        return self


__all__ = [
    "ModelLaneBriefContextPack",
    "ModelLaneBriefContextRequest",
    "ModelLaneBriefExemplarCandidate",
    "ModelLaneBriefExemplarChoice",
    "ModelLaneBriefJudgement",
    "ModelLaneBriefReviewComment",
    "ModelLaneBriefRungAttempt",
    "ModelLaneBriefSummary",
    "ModelLaneBriefTierRecord",
    "is_claude_assembler",
]
