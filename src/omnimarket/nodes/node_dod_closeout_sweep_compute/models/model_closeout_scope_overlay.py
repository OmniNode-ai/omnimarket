# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed overlay of the closeout sweep's scope ranking (OMN-20655)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class EnumRankRule(StrEnum):
    """One ordered ranking rule; the overlay lists the rules in the order they decide."""

    WAITING_ON = "waiting_on"
    HAS_CONTRACT = "has_contract"
    MERGED_IN_REVIEW = "merged_in_review"


class ModelCloseoutScopeOverlay(BaseModel):
    """Every value the scope ranking and the bundle read; none is a constant in code."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = Field(ge=1)
    eligible_states: list[str] = Field(min_length=1)
    rank_rules: list[EnumRankRule]
    no_contract_reserve: int = Field(ge=0)
    bundle_max_bytes: int = Field(ge=1024)
    held_cap: int = Field(ge=0)
    waiting_cap: int = Field(ge=0)
    contract_repos: list[str]
    default_refs: list[str] = Field(min_length=1)
    no_contract_detail_prefix: str = Field(min_length=1)
