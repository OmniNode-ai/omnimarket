# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Requests and results of the merge-sweep lane plan (OMN-20676).

The reading is what the sweep reader gathered and evaluated; the reader emits more fields than the
plan uses (host, components, unread, ...), so the reading models ignore extra keys. An empty
``controller``, ``product`` or ``owner`` object reads as absent, as the rules module read it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_READING = ConfigDict(frozen=True, extra="ignore")

DEFAULT_MAX_LANES = 8
MAX_LANES_CEILING = 12


def _empty_as_absent(value: Any) -> Any:
    """An empty mapping is absent: the rules module tested these with ``or``."""
    return None if isinstance(value, dict) and not value else value


class ModelMergeSweepOwner(BaseModel):
    """Who owns a PR now, as the claim-owner reading evaluated it."""

    model_config = _READING

    state: str | None = None
    lane: str | None = None
    claim_ts: str | None = None


class ModelMergeSweepController(BaseModel):
    """The landing controller's stall reading."""

    model_config = _READING

    stalled: bool = False
    reasons: list[str] = Field(default_factory=list)


class ModelMergeSweepProduct(BaseModel):
    """The product-merge reading; only the repositories under their floor decide a lane."""

    model_config = _READING

    under_floor: list[str] = Field(default_factory=list)


class _ModelOwned(BaseModel):
    """A reading row with an owner; an empty owner object reads as absent."""

    model_config = _READING

    @model_validator(mode="before")
    @classmethod
    def _owner_absent_when_empty(cls, data: Any) -> Any:
        if isinstance(data, dict):
            return {**data, "owner": _empty_as_absent(data.get("owner"))}
        return data


class ModelMergeSweepEscalation(_ModelOwned):
    """A PR the controller's newest degraded list names escalation_exhausted."""

    pr: str
    state: str = "UNKNOWN"
    owner: ModelMergeSweepOwner | None = None


class ModelMergeSweepChainHead(_ModelOwned):
    """An open PR on a default branch whose head branch is the base of other open PRs."""

    repo: str
    number: int
    cls: str | None = None
    state: str = "UNKNOWN"
    owner: ModelMergeSweepOwner | None = None
    children: list[int] = Field(default_factory=list)


class ModelMergeSweepRed(_ModelOwned):
    """An open PR with a red check, classified on the newest run of each check name."""

    repo: str
    number: int
    state: str = "UNKNOWN"
    ticket: str | None = None
    classes: list[dict[str, Any]] | None = None
    owner: ModelMergeSweepOwner | None = None


class ModelMergeSweepReading(BaseModel):
    """One reading of the fleet, as the sweep reader evaluated it."""

    model_config = _READING

    now: str | None = None
    load1: float | None = None
    cpus: float | None = None
    scope: list[str] | None = None
    controller: ModelMergeSweepController | None = None
    product: ModelMergeSweepProduct | None = None
    escalations: list[ModelMergeSweepEscalation] | None = None
    chain_heads: list[ModelMergeSweepChainHead] | None = None
    reds: list[ModelMergeSweepRed] | None = None

    @model_validator(mode="before")
    @classmethod
    def _absent_when_empty(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        return {
            **data,
            "controller": _empty_as_absent(data.get("controller")),
            "product": _empty_as_absent(data.get("product")),
        }


class ModelMergeSweepPlanRequest(BaseModel):
    """The reading to plan from, and the dispatch cap (None or 0 reads as the default)."""

    model_config = _FROZEN

    reading: ModelMergeSweepReading
    max_lanes: int | None = None


class ModelMergeSweepLanePr(BaseModel):
    """One PR in a lane. Keys the rules did not set stay unset; dump with ``exclude_unset``."""

    model_config = _FROZEN

    pr: str
    supersedes_claim: str | None = None
    children: list[int] = Field(default_factory=list)
    ticket: str | None = None
    classes: list[dict[str, Any]] = Field(default_factory=list)


class ModelMergeSweepLane(BaseModel):
    """One lane a sweep dispatches: diagnose, escalation, land-chain-head or fix."""

    model_config = _FROZEN

    kind: str
    repo: str | None
    prs: list[ModelMergeSweepLanePr]
    reasons: list[str] = Field(default_factory=list)


class ModelMergeSweepSkipped(BaseModel):
    """A PR the plan left out, with the reason."""

    model_config = _FROZEN

    pr: str
    why: str


class ModelMergeSweepPlanResult(BaseModel):
    """The plan: every lane in order, what was skipped, and the lanes this sweep dispatches."""

    model_config = _FROZEN

    now: str | None
    lab_only: bool
    load_per_core: float | None
    lanes: list[ModelMergeSweepLane]
    skipped: list[ModelMergeSweepSkipped]
    dispatch: list[ModelMergeSweepLane]
    deferred: int
