# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The operator-owned priority declaration and the engineering-owned rank configuration (OMN-21904).

Shape only: the values belong to the overlays ``lab_fill_priority`` (the operator's weights,
boosts that expire and excludes) and ``lab_fill_rank`` (the mode, caps, priority map and size
buckets). Every number is a quoted decimal string in the overlay, because the shared overlay
reader parses YAML with ``safe_load``, which turns an unquoted ``0.30`` into a float; a float,
an integer or a boolean where a decimal is required fails the load. Decimals become exact
``Fraction`` values so a score computed from them cannot differ by a last digit between hosts.

There is no default for ``mode`` and no default weight: an overlay that omits a key fails the
load naming the key. Nothing here reads a file, the network or the clock; ``today`` is an
argument.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from fractions import Fraction
from typing import Annotated, Any, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    PlainValidator,
    WithJsonSchema,
    model_validator,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")
_DECIMAL = re.compile(r"[0-9]+(\.[0-9]+)?")
_TICKET = r"^OMN-[0-9]+$"

# Operator decision (RULING 2026-11-01T22:49:12Z): a boost never outlives 30 days.
BOOST_EXPIRY_CEILING_DAYS = 30


def _parse_decimal(value: Any) -> Fraction:
    """A non-negative decimal written as a string; anything else fails the load."""
    if not isinstance(value, str) or _DECIMAL.fullmatch(value) is None:
        raise ValueError("must be a quoted non-negative decimal string such as '0.30'")
    return Fraction(value)


DecimalFraction = Annotated[
    Fraction,
    PlainValidator(_parse_decimal),
    PlainSerializer(lambda value: str(value), return_type=str),
    WithJsonSchema({"type": "string", "pattern": r"^[0-9]+(\.[0-9]+)?$"}),
]


def _strict_date(value: Any) -> date:
    """A calendar date (``2026-11-15``); a timestamp or a quoted string is refused."""
    if isinstance(value, datetime) or not isinstance(value, date):
        raise ValueError("must be an absolute calendar date such as 2026-11-15")
    return value


ExpiryDate = Annotated[
    date,
    PlainValidator(_strict_date),
    WithJsonSchema({"type": "string", "format": "date"}),
]


class ModelLabFillPriorityMatch(BaseModel):
    """What a boost or an exclude names: exactly one of ticket, epic, label or repo."""

    model_config = _FROZEN

    ticket: str | None = Field(default=None, pattern=_TICKET)
    epic: str | None = Field(default=None, pattern=_TICKET)
    label: str | None = Field(default=None, min_length=1)
    repo: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def _exactly_one_key(self) -> Self:
        named = [
            key
            for key in ("ticket", "epic", "label", "repo")
            if getattr(self, key) is not None
        ]
        if len(named) != 1:
            raise ValueError("a match names exactly one of ticket, epic, label, repo")
        return self


class ModelLabFillPriorityWeights(BaseModel):
    """Relative importance of the five score terms; rework is subtracted by the score."""

    model_config = _FROZEN

    unblock: DecimalFraction
    beta_gate: DecimalFraction
    throughput: DecimalFraction
    priority_age: DecimalFraction
    rework: DecimalFraction


class ModelLabFillPriorityBoost(BaseModel):
    """A judgment that always expires: valid through ``expires`` inclusive."""

    model_config = _FROZEN

    match: ModelLabFillPriorityMatch
    multiplier: DecimalFraction
    expires: ExpiryDate
    # A pointer to the row behind the boost; the loader checks nothing about it and copies nothing.
    source: str | None = None

    @model_validator(mode="after")
    def _match_kinds(self) -> Self:
        if self.match.repo is not None:
            raise ValueError("a boost matches a ticket, an epic or a label, not a repo")
        return self


class ModelLabFillPriorityExclude(BaseModel):
    """A ticket, epic or repository the factory must not touch."""

    model_config = _FROZEN

    match: ModelLabFillPriorityMatch
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def _match_kinds(self) -> Self:
        if self.match.label is not None:
            raise ValueError(
                "an exclude matches a ticket, an epic or a repo, not a label"
            )
        return self


class ModelLabFillSizeBucket(BaseModel):
    """Tickets with an estimate up to ``max_estimate`` divide the score by ``size``.

    The last bucket has no ``max_estimate`` and takes every larger estimate.
    """

    model_config = _FROZEN

    max_estimate: int | None = Field(default=None, gt=0)
    size: DecimalFraction

    @model_validator(mode="after")
    def _positive_size(self) -> Self:
        if self.size <= 0:
            raise ValueError("a size bucket must be positive")
        return self


class ModelLabFillRankConfig(BaseModel):
    """``<root>/lab_fill_rank/overlay.yaml``: engineering-owned; every key is required."""

    model_config = _FROZEN

    version: Literal[1]
    mode: Literal["off", "shadow", "on"]
    blocks_cap: int = Field(gt=0)
    age_days_cap: int = Field(gt=0)
    redispatch_cap: int = Field(gt=0)
    description_read_cap: int = Field(gt=0)
    max_boost_days: int = Field(gt=0, le=BOOST_EXPIRY_CEILING_DAYS)
    boost_multiplier_min: DecimalFraction
    boost_multiplier_max: DecimalFraction
    priority_share: DecimalFraction
    age_share: DecimalFraction
    # Linear priority number (0 is "no priority") to its score in [0, 1].
    priority_map: dict[int, DecimalFraction]
    size_buckets: tuple[ModelLabFillSizeBucket, ...]
    size_unestimated: DecimalFraction
    throughput_tags: tuple[str, ...]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        if self.priority_share > 1 or self.age_share > 1:
            raise ValueError("priority_share and age_share are shares in [0, 1]")
        if self.priority_share + self.age_share != 1:
            raise ValueError("priority_share plus age_share must sum to 1")
        if not 0 < self.boost_multiplier_min <= self.boost_multiplier_max:
            raise ValueError(
                "boost_multiplier_min must be positive and at most boost_multiplier_max"
            )
        if sorted(self.priority_map) != [0, 1, 2, 3, 4]:
            raise ValueError("priority_map must hold exactly the keys 0, 1, 2, 3, 4")
        if any(score > 1 for score in self.priority_map.values()):
            raise ValueError("priority_map scores are in [0, 1]")
        if self.size_unestimated <= 0:
            raise ValueError("size_unestimated must be positive")
        if not self.size_buckets:
            raise ValueError("size_buckets must hold at least one bucket")
        *bounded, last = self.size_buckets
        if last.max_estimate is not None:
            raise ValueError("the last size bucket takes every larger estimate")
        limits = [bucket.max_estimate for bucket in bounded]
        given = [limit for limit in limits if limit is not None]
        if len(given) != len(limits) or given != sorted(set(given)):
            raise ValueError(
                "size bucket estimates must be given and strictly increasing"
            )
        return self


class ModelLabFillPriorityDeclaration(BaseModel):
    """``<root>/lab_fill_priority/overlay.yaml``: the one file the operator edits.

    Nothing computed lives here: no score, no rank, no ticket order.
    """

    model_config = _FROZEN

    version: Literal[1]
    weights: ModelLabFillPriorityWeights
    boost: tuple[ModelLabFillPriorityBoost, ...]
    exclude: tuple[ModelLabFillPriorityExclude, ...]

    def field_problems(self, config: ModelLabFillRankConfig, today: date) -> list[str]:
        """Dotted field paths that break the rank configuration's bounds, never values."""
        horizon = today + timedelta(days=config.max_boost_days)
        problems: list[str] = []
        for index, boost in enumerate(self.boost):
            if boost.expires > horizon:
                problems.append(f"boost[{index}].expires")
            if not (
                config.boost_multiplier_min
                <= boost.multiplier
                <= config.boost_multiplier_max
            ):
                problems.append(f"boost[{index}].multiplier")
        return problems

    def active_boosts(self, today: date) -> tuple[ModelLabFillPriorityBoost, ...]:
        """The boosts valid on ``today``: ``expires`` is inclusive."""
        return tuple(boost for boost in self.boost if boost.expires >= today)

    def boosts_expired(self, today: date) -> int:
        """How many boosts are ignored because ``expires`` is before ``today``."""
        return len(self.boost) - len(self.active_boosts(today))
