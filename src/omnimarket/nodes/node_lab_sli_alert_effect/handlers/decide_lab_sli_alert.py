# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Act on lab indicator breaches per severity, deduplicated per indicator.

Canonical definition-B shape: ``handle(ModelLabSliAlertRequest) ->
ModelLabSliAlertResult``. Pure: the alert state comes in on the request and
goes out on the result; the clock is the evaluation's own interval start.

What a severity does is ``config.lab_sli_alert.severity_policy`` in the
contract; nothing here names a severity or a renotify period.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from omnimarket.enums.enum_lab_sli import (
    EnumLabSliActionKind,
    EnumLabSliSeverity,
    EnumLabSliStatus,
)
from omnimarket.models.lab_sli import (
    ModelLabSliAlertAction,
    ModelLabSliAlertRequest,
    ModelLabSliAlertResult,
    ModelLabSliAlertState,
    ModelLabSliEvaluated,
)

CONTRACT_PATH = Path(__file__).resolve().parent.parent / "contract.yaml"


class ModelSeverityPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    push_operator: bool
    fix_lane: Literal["immediate", "batched", "none"]
    renotify_minutes: int = Field(ge=1)


class ModelLabSliAlertPolicy(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lane_prefix: str = Field(min_length=1)
    batched_lane_slug: str = Field(min_length=1)
    unknown_acts_as: EnumLabSliSeverity
    severity_policy: dict[EnumLabSliSeverity, ModelSeverityPolicy]


def load_alert_policy(path: Path = CONTRACT_PATH) -> ModelLabSliAlertPolicy:
    """Read ``config.lab_sli_alert``; fail closed on a missing severity."""
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    block = (raw.get("config") or {}).get("lab_sli_alert")
    if not isinstance(block, dict):
        raise ValueError(f"{path}: config.lab_sli_alert is missing")
    policy = ModelLabSliAlertPolicy.model_validate(block)
    missing = set(EnumLabSliSeverity) - set(policy.severity_policy)
    if missing:
        raise ValueError(f"{path}: severity_policy lacks {sorted(missing)}")
    return policy


@lru_cache(maxsize=1)
def _contract_policy() -> ModelLabSliAlertPolicy:
    return load_alert_policy()


def _stamp(at: datetime) -> str:
    return at.strftime("%Y%m%dT%H%MZ")


def _describe(evaluation: ModelLabSliEvaluated) -> str:
    if evaluation.status is EnumLabSliStatus.UNKNOWN:
        return f"{evaluation.sli_id} is dark ({evaluation.detail})"
    subjects = ", ".join(evaluation.breaching_subjects) or evaluation.worst_subject
    return (
        f"{evaluation.sli_id}: {evaluation.value:g} {evaluation.unit} at {subjects}"
        f" (target {evaluation.comparator.value} {evaluation.target:g})"
    )


class DecideLabSliAlert:
    """Severity-driven actions for one interval, with per-indicator dedup. Pure."""

    def __init__(self, policy: ModelLabSliAlertPolicy | None = None) -> None:
        self._policy = policy if policy is not None else _contract_policy()

    def _acting_severity(
        self, evaluation: ModelLabSliEvaluated
    ) -> EnumLabSliSeverity | None:
        if evaluation.status is EnumLabSliStatus.BREACH:
            return evaluation.severity
        if evaluation.status is EnumLabSliStatus.UNKNOWN:
            return self._policy.unknown_acts_as
        return None

    def handle(self, request: ModelLabSliAlertRequest) -> ModelLabSliAlertResult:
        now = request.evaluation.interval_start
        last_fired = dict(request.state.last_fired)
        firing = dict(request.state.firing)
        actions: list[ModelLabSliAlertAction] = []
        batched: list[ModelLabSliEvaluated] = []
        suppressed: list[str] = []
        cleared: list[str] = []

        for evaluation in request.evaluation.evaluations:
            sli_id = evaluation.sli_id
            severity = self._acting_severity(evaluation)
            if severity is None:
                if sli_id in firing:
                    cleared.append(sli_id)
                    firing.pop(sli_id, None)
                    last_fired.pop(sli_id, None)
                continue
            rule = self._policy.severity_policy[severity]
            if rule.fix_lane == "none" and not rule.push_operator:
                continue
            previous = last_fired.get(sli_id)
            if (
                sli_id in firing
                and previous is not None
                and now - previous < timedelta(minutes=rule.renotify_minutes)
            ):
                suppressed.append(sli_id)
                continue
            firing[sli_id] = severity
            last_fired[sli_id] = now
            text = (
                f"{severity.value} {_describe(evaluation)}. Action: {evaluation.action}"
            )
            key = f"lab-sli:{sli_id}:{_stamp(now)}"
            if rule.push_operator:
                actions.append(
                    ModelLabSliAlertAction(
                        kind=EnumLabSliActionKind.PUSH_OPERATOR,
                        severity=severity,
                        sli_ids=(sli_id,),
                        dedup_key=f"{key}:push",
                        text=text,
                    )
                )
            if rule.fix_lane == "immediate":
                slug = sli_id.replace("_", "-")
                actions.append(
                    ModelLabSliAlertAction(
                        kind=EnumLabSliActionKind.FIX_LANE,
                        severity=severity,
                        sli_ids=(sli_id,),
                        dedup_key=f"{key}:lane",
                        lane=f"{self._policy.lane_prefix}{slug}-{_stamp(now)}",
                        text=text,
                    )
                )
            elif rule.fix_lane == "batched":
                batched.append(evaluation)

        if batched:
            ids = tuple(e.sli_id for e in batched)
            worst = min(
                (firing[i] for i in ids),
                key=lambda s: list(EnumLabSliSeverity).index(s),
            )
            actions.append(
                ModelLabSliAlertAction(
                    kind=EnumLabSliActionKind.BATCHED_FIX_LANE,
                    severity=worst,
                    sli_ids=ids,
                    dedup_key=f"lab-sli:batched:{_stamp(now)}",
                    lane=f"{self._policy.lane_prefix}{self._policy.batched_lane_slug}-{_stamp(now)}",
                    text="\n".join(
                        f"- {firing[e.sli_id].value} {_describe(e)}. Action: {e.action}"
                        for e in batched
                    ),
                )
            )

        return ModelLabSliAlertResult(
            interval_start=now,
            actions=tuple(actions),
            suppressed=tuple(suppressed),
            cleared=tuple(cleared),
            state=ModelLabSliAlertState(last_fired=last_fired, firing=firing),
        )


__all__ = ["DecideLabSliAlert", "ModelLabSliAlertPolicy", "load_alert_policy"]
