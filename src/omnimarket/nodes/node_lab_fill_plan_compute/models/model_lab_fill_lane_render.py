# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""A brief and structured launch keep effect-side quoting out of the decision."""

import math
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from omnimarket.models.lab_fill import ModelLabFillLaunch

from .model_lab_fill_plan import _CAMEL, ModelLabFillDispatchItem


class ModelLabFillRenderConfig(BaseModel):
    """The task-specific authority is required because a lane cannot act without it."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    pillar: str
    parent_lane: str
    parent_ticket: str
    run_key: str
    authority_ruling: str
    authority_lane: str
    landing_lane: str = "landing-controller"
    lane_timeout_min: int = Field(default=60, ge=1, le=180)
    stalled_hours: int = Field(default=6, ge=1, le=168)

    @field_validator("lane_timeout_min", "stalled_hours", mode="before")
    @classmethod
    def _positive_int(cls, value: object, info: ValidationInfo) -> int:
        """parseArgs applies defaults and caps after Number() validates a positive integer."""
        name = info.field_name or ""
        default, cap = (60, 180) if name == "lane_timeout_min" else (6, 168)
        if value is None or value == "":
            return default
        try:
            number = (
                float(value) if isinstance(value, (str, int, float)) else float("nan")
            )
        except ValueError:
            number = float("nan")
        if not math.isfinite(number) or number < 1 or not number.is_integer():
            raise ValueError(f"{name} must be a positive integer")
        return min(int(number), cap)

    @field_validator(
        "pillar",
        "parent_lane",
        "parent_ticket",
        "authority_ruling",
        "authority_lane",
        "landing_lane",
        "run_key",
    )
    @classmethod
    def _tokens(cls, value: str, info: ValidationInfo) -> str:
        # Field names, rather than deployment identities, select the syntax.
        name = info.field_name or ""
        patterns = {
            "pillar": r"[a-z][a-z0-9-]*",
            "parent_ticket": r"OMN-[0-9]+",
            "authority_ruling": r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z",
            "run_key": r"[0-9]{4}-[0-9]{2}-[0-9]{2}(T[0-9]{4}Z)?",
        }
        value = value.strip()
        if (
            re.fullmatch(patterns.get(name, r"[A-Za-z0-9][A-Za-z0-9_.:-]*"), value)
            is None
        ):
            raise ValueError(f"invalid {name}: '{value}'")
        return value


class ModelLabFillFallbackItem(BaseModel):
    """The same task gets one Sonnet retry after Codex setup failed."""

    model_config = _CAMEL
    lane: str
    from_lane: str = Field(alias="from_lane")
    from_engine: str = Field(alias="from_engine")
    from_host: str = Field(alias="from_host")
    engine: str
    reason: str
    route: str
    hosts: tuple[str, ...]
    pinned: bool
    kind: str
    id: str | None = None
    ticket: str
    pr: str
    repo: str
    ref: str
    title: str
    updated_at: str
    fallback_of: str = Field(alias="fallback_of")
    host: str | None = None


class ModelLabFillLaneRenderRequest(BaseModel):
    """Live limit markers are supplied to choose a pinned fallback's host."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    item: ModelLabFillDispatchItem | ModelLabFillFallbackItem
    config: ModelLabFillRenderConfig
    limited: tuple[str, ...] = ()


class ModelLabFillLaneRenderResult(BaseModel):
    """Exact task text alongside its pure launch decisions."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    brief: str
    launch: ModelLabFillLaunch
