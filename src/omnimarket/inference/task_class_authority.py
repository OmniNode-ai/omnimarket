# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed access to the Market-owned task-class and Gateway exposure authority."""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

_DEFAULT_AUTHORITY_PATH = (
    Path(__file__).resolve().parent.parent / "configs" / "task_class_contracts.v1.yaml"
)


class EnumGatewayExposure(StrEnum):
    """Closed Gateway exposure policy for a Market task class."""

    PUBLIC = "public"
    INTERNAL = "internal"


class EnumQualityRuleEnforcement(StrEnum):
    """Who a declared quality rule answers to (OMN-18295).

    A rule is one of these, never both — that duality is the defect this enum
    exists to make unrepresentable.
    """

    #: Contributes to the graded score; the task class's ``required_bar`` is
    #: the single authority on the verdict.
    SCORED = "scored"
    #: Vetoes acceptance outright, whatever the score. Floors and adequacy
    #: authorities only.
    BLOCKING = "blocking"


class ModelQualityRule(BaseModel):
    """One declared quality rule: its threshold and its enforcement class."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enforcement: EnumQualityRuleEnforcement
    threshold: int | None = Field(
        default=None,
        description=(
            "The rule's numeric threshold, where it has one. Declared here so "
            "a customer is never held to a number that appears in no contract "
            "and on no receipt (OMN-18295)."
        ),
    )
    threshold_unit: str | None = Field(
        default=None,
        description="What the threshold counts — 'words', 'characters'.",
    )
    rationale: str = Field(
        min_length=1,
        description=(
            "Why this rule is scored or blocking. Required: an enforcement "
            "class with no stated reason is the thing that drifts."
        ),
    )


class ModelReasoningPreamblePolicy(BaseModel):
    """Where a leaked plain-text reasoning scratchpad ends (OMN-18379).

    The search order and the reason each entry exists are documented in the
    ``reasoning_preamble`` block of ``task_class_contracts.v1.yaml``. The
    values live there rather than in Python so the phrases a customer's
    response is segmented on are a readable contract.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    lead_in_phrases: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "Phrases that, at the START of a response, mark it as opening with "
            "a reasoning scratchpad. Required before the structural boundaries "
            "(marker / header / fence) are consulted at all."
        ),
    )
    answer_markers: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "Explicit answer markers a prompt can request. A line equal to one "
            "of these ends the preamble."
        ),
    )
    closing_trace_tags: tuple[str, ...] = Field(
        min_length=1,
        description=(
            "Reasoning-trace terminators. One with no matching opener ends the "
            "preamble on its own, with no lead-in required."
        ),
    )
    rationale: str = Field(
        min_length=1,
        description="Why this policy is shaped the way it is.",
    )


class ModelTaskClassSelection(BaseModel):
    """How a prompt selects this task class (OMN-18305).

    This is the contract half of task-class SELECTION. Before OMN-18305 there
    was no contract half at all: the `onex delegate` CLI carried a hardcoded,
    ordered keyword table lifted from retired skill markdown, matched with a
    bare substring test, first rule wins. A 56 KB engineering standup was
    therefore filed as a test-writing task because the material it summarised
    contained the word "test" — and `test` is not one of the classes whose
    quality bar arms the prose checks, so the identifier-grounding check and
    the prose quality band never ran on the customer's answer. The four-word
    prompt "the latest window" classified as `test` for the same reason:
    "latest" contains "test".

    Three properties are load-bearing, and each is a field here rather than an
    implementation detail of whichever consumer evaluates it:

    * **Presence, never frequency.** A phrase either occurs or it does not.
      Counting occurrences is what let the bulk of a document outvote its
      purpose.
    * **Word boundaries.** Phrases match on word boundaries only, so no phrase
      can match inside a longer word.
    * **Shape gates the keyword.** ``min_words`` / ``max_words`` bound the
      prompt shapes a class is eligible for at all, evaluated BEFORE any
      phrase. A long prose task is structurally ineligible for the short
      keyword-driven classes, whatever words it happens to contain.

    ``priority`` breaks ties between eligible classes (higher wins; equal
    priorities are broken by class name so resolution is total and
    deterministic). An empty ``phrases`` list means this class is never
    resolved from a prompt — the explicit, typed way to say so for the
    internal classes.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    priority: int = Field(
        ge=0,
        description=(
            "Rank among eligible classes; higher wins. The prose classes sit "
            "above the keyword classes so prompt shape outranks one "
            "incidental keyword."
        ),
    )
    phrases: tuple[str, ...] = Field(
        description=(
            "Word-boundary phrases whose PRESENCE makes this class eligible. "
            "Empty means never selected from a prompt."
        ),
    )
    min_words: int | None = Field(
        default=None,
        ge=1,
        description="Shortest prompt, in words, this class is eligible for.",
    )
    max_words: int | None = Field(
        default=None,
        ge=1,
        description=(
            "Longest prompt, in words, this class is eligible for. This is "
            "the bound that makes a 7,000-word ledger structurally ineligible "
            "for `test`."
        ),
    )

    @field_validator("phrases")
    @classmethod
    def _validate_phrases(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        invalid = sorted(
            phrase for phrase in value if not phrase or phrase != phrase.strip().lower()
        )
        if invalid:
            raise ValueError(
                f"selection phrases must be non-empty, trimmed and lowercase: {invalid}"
            )
        return value


class ModelTaskClassAuthorityEntry(BaseModel):
    """Authority fields shared by every task-class routing contract entry."""

    model_config = ConfigDict(frozen=True, extra="allow")

    gateway_exposure: EnumGatewayExposure
    selection: ModelTaskClassSelection


class ModelTaskClassAuthority(BaseModel):
    """Market-owned task-class universe with a total Gateway exposure partition."""

    model_config = ConfigDict(frozen=True, extra="allow")

    task_classes: dict[str, ModelTaskClassAuthorityEntry] = Field(min_length=1)
    quality_rules: dict[str, ModelQualityRule] = Field(default_factory=dict)
    reasoning_preamble: ModelReasoningPreamblePolicy | None = Field(
        default=None,
        description=(
            "How a leaked reasoning scratchpad is separated from the answer "
            "(OMN-18379). ``None`` means no segmentation is performed and the "
            "whole response is verified, which is the pre-ticket behaviour."
        ),
    )

    @field_validator("task_classes")
    @classmethod
    def _validate_task_class_names(
        cls,
        value: dict[str, ModelTaskClassAuthorityEntry],
    ) -> dict[str, ModelTaskClassAuthorityEntry]:
        invalid = sorted(name for name in value if not name or name != name.strip())
        if invalid:
            raise ValueError(
                f"task class names must be non-empty and trimmed: {invalid}"
            )
        return value

    @property
    def universe(self) -> frozenset[str]:
        """Return the registry keys, which are the sole task-class denominator."""
        return frozenset(self.task_classes)

    @property
    def public_task_classes(self) -> frozenset[str]:
        """Return the exact public Gateway projection."""
        return self._classes_with_exposure(EnumGatewayExposure.PUBLIC)

    @property
    def internal_task_classes(self) -> frozenset[str]:
        """Return valid Market classes excluded from the public Gateway."""
        return self._classes_with_exposure(EnumGatewayExposure.INTERNAL)

    def _classes_with_exposure(
        self,
        exposure: EnumGatewayExposure,
    ) -> frozenset[str]:
        return frozenset(
            name
            for name, entry in self.task_classes.items()
            if entry.gateway_exposure is exposure
        )


def load_task_class_authority(
    config_path: str | Path | None = None,
) -> ModelTaskClassAuthority:
    """Load the Market authority and fail closed on missing/unknown exposure."""
    path = Path(config_path) if config_path is not None else _DEFAULT_AUTHORITY_PATH
    if not path.is_file():
        raise FileNotFoundError(f"task-class authority not found at {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"task-class authority root must be a mapping: {path}")
    try:
        return ModelTaskClassAuthority.model_validate(raw)
    except ValidationError as exc:
        msg = f"task-class authority validation failed for {path}: {exc}"
        raise ValueError(msg) from exc


@lru_cache(maxsize=1)
def _quality_rules() -> dict[str, ModelQualityRule]:
    """The declared quality-rule registry, read once.

    Cached because the quality gate is a pure reducer called per delegation
    and per declared check: re-reading and re-validating the authority file
    inside that loop would put file I/O on a hot path that is meant to be
    deterministic and free.

    A missing or malformed authority file yields an EMPTY registry rather than
    raising. The gate's job is to judge a response, and it must not start
    failing every delegation because a config read broke — see
    :func:`resolve_quality_rule` for what an unresolved rule then means.
    """
    try:
        return dict(load_task_class_authority().quality_rules)
    except (FileNotFoundError, ValueError):
        return {}


def resolve_quality_rule(name: str) -> ModelQualityRule | None:
    """Return the declared rule for ``name``, or ``None`` if undeclared.

    ``None`` is a real answer, not an error: a check the quality gate knows
    how to run but the contract has not classified. The gate treats an
    unresolved rule as BLOCKING — the pre-OMN-18295 behaviour for every
    heuristic check. Fail-closed is the right default here because the
    alternative is that forgetting to declare a rule silently strips its
    veto, which is precisely the class of quiet weakening this ticket is
    about.
    """
    return _quality_rules().get(name)


@lru_cache(maxsize=1)
def resolve_reasoning_preamble_policy() -> ModelReasoningPreamblePolicy | None:
    """The declared reasoning-preamble segmentation policy, or ``None``.

    ``None`` is a real answer: the authority file is absent, unreadable, or
    declares no ``reasoning_preamble`` block. The segmenter then finds no
    boundary, the whole response is verified exactly as it was before
    OMN-18379, and the receipt says ``no_boundary_found``. Failing closed here
    means NOT cutting: a segmenter that guessed a boundary from an unreadable
    contract would silently drop a customer's answer, which is a far worse
    outcome than the veto this ticket exists to remove.

    Cached for the same reason ``_quality_rules`` is: the gate is a pure
    reducer called per delegation and per declared check.
    """
    try:
        return load_task_class_authority().reasoning_preamble
    except (FileNotFoundError, ValueError):
        return None


__all__ = [
    "EnumGatewayExposure",
    "EnumQualityRuleEnforcement",
    "ModelQualityRule",
    "ModelReasoningPreamblePolicy",
    "ModelTaskClassAuthority",
    "ModelTaskClassAuthorityEntry",
    "load_task_class_authority",
    "resolve_quality_rule",
    "resolve_reasoning_preamble_policy",
]
