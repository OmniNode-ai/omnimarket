# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Request and result of node_review_finding_veto_effect (OMN-17427)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ModelFindingVeto(BaseModel):
    """One blocking finding dropped because the file it condemns parses."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    file_path: str = Field(description="Path the finding was reported against.")
    line_start: int = Field(description="Line the finding was reported at.")
    language: str = Field(description="python, yaml or json.")
    message: str = Field(description="The finding's representative message.")
    reason: str = Field(description="Why the finding was dropped, with the head.")


class ModelReviewFindingVetoRequest(BaseModel):
    """The reviewer's result document and the head its findings were raised on."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: dict[str, Any] = Field(
        description="The reviewer's ModelMultiReviewResult document, as parsed JSON."
    )
    head_sha: str = Field(
        min_length=7, description="The pull request head the reviewers read."
    )


class ModelReviewFindingVetoResult(BaseModel):
    """The result document with disproved blocking findings removed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    result: dict[str, Any] = Field(
        description="The reviewer's document; unchanged when nothing was vetoed."
    )
    vetoed: tuple[ModelFindingVeto, ...] = Field(
        default=(), description="Blocking findings dropped, each with its reason."
    )
    kept_unverified: tuple[str, ...] = Field(
        default=(),
        description=(
            "Syntax claims that stayed blocking: the file did not parse, or could "
            "not be read at the head, or the claim was not provably about it."
        ),
    )


__all__: list[str] = [
    "ModelFindingVeto",
    "ModelReviewFindingVetoRequest",
    "ModelReviewFindingVetoResult",
]
