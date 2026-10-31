# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Typed request and report of the receipt-gate pin friction comparison (OMN-20134)."""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from pydantic import BaseModel, ConfigDict, Field, model_validator

from omnimarket.models.receipt_gate_pin_friction import (
    AwareTime,
    EnumPinKind,
    EnumPinRefusalClass,
    ModelPinFrictionRow,
)


class ModelPinFrictionReportRequest(BaseModel):
    """The projection's rows and the window to compare over.

    The window selects the cohort: rows first seen in ``[window_start, window_end)``.
    Every count, rate and median below is over that cohort, so the two pin kinds are
    compared on the pull requests the updater met in the same period.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    rows: tuple[ModelPinFrictionRow, ...]
    window_start: AwareTime
    window_end: AwareTime

    @model_validator(mode="after")
    def _window_is_ordered(self) -> ModelPinFrictionReportRequest:
        if self.window_end <= self.window_start:
            raise ValueError("window_end must be after window_start")
        return self


class ModelRefusalCount(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    refusal_class: EnumPinRefusalClass
    count: int = Field(ge=0)


class ModelPinKindSummary(BaseModel):
    """One pin kind over the window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pin_kind: EnumPinKind
    prs_opened: int = Field(ge=0, description="Pull requests the updater opened.")
    merged: int = Field(ge=0)
    merged_untouched: int = Field(
        ge=0, description="Merged with no fix push and no CI re-run."
    )
    still_open: int = Field(ge=0)
    closed_unmerged: int = Field(ge=0)
    median_seconds_pr_open_to_merge: float | None = None
    median_seconds_tag_to_merge: float | None = None
    fix_pushes: int = Field(ge=0)
    ci_reruns: int = Field(ge=0)
    touches_per_pr: float | None = Field(
        default=None, description="Fix pushes plus CI re-runs, per pull request opened."
    )
    refusals: int = Field(ge=0, description="Refusals recorded on the cohort's rows.")
    refusals_by_class: tuple[ModelRefusalCount, ...] = ()
    updater_failures: int = Field(
        ge=0, description="Callers the updater failed to open or refused to change."
    )

    @property
    def untouched_rate(self) -> float | None:
        return None if self.prs_opened == 0 else self.merged_untouched / self.prs_opened


class ModelPinFrictionReport(BaseModel):
    """Per pin kind, over a window: the comparison the ruling asked to keep measuring."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    window_start: AwareTime
    window_end: AwareTime
    rows_in_window: int = Field(ge=0)
    kinds: tuple[ModelPinKindSummary, ...]

    def render(self) -> str:
        """A fixed-width table, one column per pin kind, one line per measure."""
        header = (
            f"pin friction {self.window_start:%Y-%m-%dT%H:%M:%SZ} .. "
            f"{self.window_end:%Y-%m-%dT%H:%M:%SZ} ({self.rows_in_window} rows)"
        )
        lines = [
            header,
            f"{'measure':<26}" + "".join(f"{k.pin_kind.value:>12}" for k in self.kinds),
        ]
        measures: list[tuple[str, Callable[[ModelPinKindSummary], str]]] = [
            ("PRs opened", lambda k: str(k.prs_opened)),
            ("merged", lambda k: str(k.merged)),
            ("merged untouched", lambda k: str(k.merged_untouched)),
            ("untouched rate", lambda k: _pct(k.untouched_rate)),
            ("still open", lambda k: str(k.still_open)),
            (
                "median open to merge",
                lambda k: _duration(k.median_seconds_pr_open_to_merge),
            ),
            ("median tag to merge", lambda k: _duration(k.median_seconds_tag_to_merge)),
            ("fix pushes", lambda k: str(k.fix_pushes)),
            ("CI re-runs", lambda k: str(k.ci_reruns)),
            (
                "touches per PR",
                lambda k: (
                    "-" if k.touches_per_pr is None else f"{k.touches_per_pr:.2f}"
                ),
            ),
            ("refusals", lambda k: str(k.refusals)),
            *((f"  {c.value}", partial(_refusals_of, c)) for c in EnumPinRefusalClass),
            ("updater failures", lambda k: str(k.updater_failures)),
        ]
        for label, measure in measures:
            lines.append(
                f"{label:<26}" + "".join(f"{measure(k):>12}" for k in self.kinds)
            )
        return "\n".join(lines)


def _refusals_of(refusal_class: EnumPinRefusalClass, kind: ModelPinKindSummary) -> str:
    return str(
        next(
            (
                r.count
                for r in kind.refusals_by_class
                if r.refusal_class is refusal_class
            ),
            0,
        )
    )


def _pct(value: float | None) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 3600:
        return f"{seconds / 60:.1f}m"
    return f"{seconds / 3600:.1f}h"


__all__ = [
    "ModelPinFrictionReport",
    "ModelPinFrictionReportRequest",
    "ModelPinKindSummary",
    "ModelRefusalCount",
]
