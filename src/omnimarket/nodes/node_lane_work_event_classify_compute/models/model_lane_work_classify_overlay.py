# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The classifier's overlay: an ordered pattern table over worker text, and the CI verdict words."""

from __future__ import annotations

import re
from typing import Literal

from omnibase_core.enums.enum_lane_work_event import EnumLaneWorkEvent
from omnibase_core.models.lane_work.model_lane_work_event import EVENT_CLASSES
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from omnimarket.nodes.node_ci_red_kind_rule_compute.models.model_ci_red_kind_rule_overlay import (
    ModelCiRedKindRuleOverlay,
)

# The kinds a worker's text can name; every other kind has a typed producer.
TEXT_EVENTS = frozenset(
    {
        EnumLaneWorkEvent.HEAD_MOVED,
        EnumLaneWorkEvent.LOG_READ,
        EnumLaneWorkEvent.CAUSE_FOUND,
        EnumLaneWorkEvent.CAUSE_UNKNOWN,
        EnumLaneWorkEvent.FIX_COMMITTED,
        EnumLaneWorkEvent.COMMIT_REFUSED,
        EnumLaneWorkEvent.PUSHED,
        EnumLaneWorkEvent.PUSH_REFUSED,
    }
)
CI_VERDICT_EVENTS = frozenset(
    {
        EnumLaneWorkEvent.CHECK_RED,
        EnumLaneWorkEvent.CHECK_PENDING,
        EnumLaneWorkEvent.CHECK_GREEN,
    }
)


class ModelLaneWorkClassifyRule(BaseModel):
    """One text rule: the first whose pattern is found in the raw result names the event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    pattern: str = Field(..., min_length=1)
    event: EnumLaneWorkEvent
    event_class: str | None = Field(default=None, min_length=1)
    ignore_case: bool
    red_kind_check_name: str | None = Field(
        default=None,
        min_length=1,
        description="log_read only: the check name put to the red kind rule node; its log excerpt is the raw result.",
    )

    @field_validator("pattern")
    @classmethod
    def _compilable(cls, value: str) -> str:
        try:
            re.compile(value)
        except re.error as exc:
            msg = f"pattern {value!r} is not a compilable regular expression: {exc}"
            raise ValueError(msg) from None
        return value

    @model_validator(mode="after")
    def _event_and_class(self) -> ModelLaneWorkClassifyRule:
        if self.event not in TEXT_EVENTS:
            msg = f"event {self.event.value!r} cannot be named from worker text"
            raise ValueError(msg)
        allowed = EVENT_CLASSES.get(self.event)
        if allowed is None:
            if self.event_class is not None:
                msg = f"event {self.event.value!r} carries no event_class"
                raise ValueError(msg)
        elif self.event_class not in allowed:
            msg = f"event_class {self.event_class!r} is not one of {sorted(allowed)} for event {self.event.value!r}"
            raise ValueError(msg)
        if (
            self.red_kind_check_name is not None
            and self.event is not EnumLaneWorkEvent.LOG_READ
        ):
            msg = "red_kind_check_name belongs to a log_read rule"
            raise ValueError(msg)
        return self


class ModelLaneWorkClassifyOverlay(BaseModel):
    """Rules are tried in declared order; ``ci_verdict_events`` maps a PR observation's verdict word to its check event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["lane_work_classify.v1"]
    rules: tuple[ModelLaneWorkClassifyRule, ...] = Field(..., min_length=1)
    ci_verdict_events: dict[str, EnumLaneWorkEvent] = Field(default_factory=dict)
    red_kind_bootstrap: ModelCiRedKindRuleOverlay | None = Field(
        default=None,
        description="Red kind rules read only when node_ci_red_kind_rule_compute has no overlay bound.",
    )

    @field_validator("ci_verdict_events")
    @classmethod
    def _check_events(
        cls, value: dict[str, EnumLaneWorkEvent]
    ) -> dict[str, EnumLaneWorkEvent]:
        bad = sorted(v.value for v in value.values() if v not in CI_VERDICT_EVENTS)
        if bad:
            msg = f"ci_verdict_events may map only to check events, got {bad}"
            raise ValueError(msg)
        return value


__all__: list[str] = [
    "CI_VERDICT_EVENTS",
    "TEXT_EVENTS",
    "ModelLaneWorkClassifyOverlay",
    "ModelLaneWorkClassifyRule",
]
