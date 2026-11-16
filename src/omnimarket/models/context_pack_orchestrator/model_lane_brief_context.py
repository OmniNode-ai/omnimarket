# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of the lane-brief context pack: seven tiers, the rungs each model-selected tier tried, and a hash."""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.enums.enum_lane_brief_tier import (
    EnumLaneBriefRung,
    EnumLaneBriefTier,
    EnumLaneBriefTierMode,
)


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


class ModelLaneBriefExemplarChoice(BaseModel):
    """What a rung chose: the path of the closest example and the shape excerpt cut from it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    path: str = Field(min_length=1)
    excerpt: str = Field(min_length=1)


class ModelLaneBriefContextRequest(BaseModel):
    """What the lane will do and the facts the deterministic tiers are read from."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ticket: str = Field(min_length=1)
    lane: str = Field(min_length=1)
    task: str = Field(min_length=1)
    profile_name: str = Field(default="lane_brief_context", min_length=1)
    files_to_touch: tuple[str, ...] = ()
    review_comments: tuple[ModelLaneBriefReviewComment, ...] = ()
    exemplar_candidates: tuple[ModelLaneBriefExemplarCandidate, ...] = ()
    ledger_rows: tuple[str, ...] = ()

    @field_validator("files_to_touch")
    @classmethod
    def _no_blank_path(cls, paths: tuple[str, ...]) -> tuple[str, ...]:
        if any(not p.strip() for p in paths):
            raise ValueError("files_to_touch names a blank path; every path is a file")
        return paths


class ModelLaneBriefRungAttempt(BaseModel):
    """One rung a model-selected tier tried and what came of it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    rung: EnumLaneBriefRung
    outcome: Literal["selected", "fell_back"]
    reason: str | None = None

    @model_validator(mode="after")
    def _a_fallback_says_why(self) -> ModelLaneBriefRungAttempt:
        if self.outcome == "fell_back" and not (self.reason or "").strip():
            raise ValueError("a rung that fell back records why")
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

    @model_validator(mode="after")
    def _mode_agrees_with_calls(self) -> ModelLaneBriefTierRecord:
        if self.mode is EnumLaneBriefTierMode.DETERMINISTIC:
            if self.model_called or self.rungs:
                raise ValueError(f"deterministic tier {self.tier.value} called a model")
            return self
        outcomes = [a.outcome for a in self.rungs]
        if not self.model_called or not self.rungs or outcomes[-1] != "selected":
            raise ValueError(
                f"model-selected tier {self.tier.value} ends on a selected rung"
            )
        if "selected" in outcomes[:-1]:
            raise ValueError(
                f"tier {self.tier.value} kept walking after a rung selected"
            )
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
    "ModelLaneBriefReviewComment",
    "ModelLaneBriefRungAttempt",
    "ModelLaneBriefTierRecord",
]
