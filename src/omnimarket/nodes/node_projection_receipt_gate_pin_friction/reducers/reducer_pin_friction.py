# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure fold of the pin friction measurement (OMN-20134).

``fold_pin_observation(row, observation, config)`` returns the pin pull request's
next row and the friction events that observation newly caused. No clock, no I/O:
every timestamp comes from the observation, so a replay derives the same row.

What the fold counts, and what it deliberately does not:

* a **fix push** is a head sha on the pin branch other than the one the updater
  published, which also counts a branch update the landing controller makes;
* a **CI re-run** is a workflow run on one of the pull request's heads whose
  attempt is above 1, counted once per (run, attempt);
* a pin pull request is **untouched** when it merged with neither. The fold has no
  actor on those observations, so a touch is a lane's or a person's or the
  controller's: the counts say how much work followed the updater, not whose.

An observation for a branch the projection holds no row for is the caller's to
drop (the pin branch is created by the updater, so a branch it never reported is
not a pin pull request).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

from omnimarket.models.receipt_gate_pin_friction import (
    EnumPinFrictionReason,
    EnumPinOutcomeKind,
    EnumPinPrState,
    EnumPinRefusalClass,
    ModelPinCheckObservation,
    ModelPinFrictionEvent,
    ModelPinFrictionRow,
    ModelPinHeadObservation,
    ModelPinMergedObservation,
    ModelPinOutcomeEvent,
    ModelPinRefusal,
    ModelPinRunObservation,
)
from omnimarket.nodes.node_projection_receipt_gate_pin_friction.models import (
    ModelPinFrictionConfig,
)

PinObservation = (
    ModelPinOutcomeEvent
    | ModelPinMergedObservation
    | ModelPinHeadObservation
    | ModelPinRunObservation
    | ModelPinCheckObservation
)
_FAILED_STATES = frozenset({EnumPinPrState.FAILED, EnumPinPrState.REFUSED})
_TERMINAL_STATES = frozenset({EnumPinPrState.MERGED, EnumPinPrState.CLOSED})


@dataclass(frozen=True)
class FoldResult:
    """The next row (None when the observation concerns no known pin pull request) and its friction."""

    row: ModelPinFrictionRow | None
    friction: tuple[ModelPinFrictionEvent, ...] = ()

    @property
    def changed(self) -> bool:
        return self.row is not None


def fold_pin_observation(
    row: ModelPinFrictionRow | None,
    observation: PinObservation,
    config: ModelPinFrictionConfig,
) -> FoldResult:
    """Apply one observation to the pin pull request's row."""
    if isinstance(observation, ModelPinOutcomeEvent):
        after = _outcome(row, observation)
        at = observation.outcome_at
    elif row is None:
        return FoldResult(row=None)
    elif isinstance(observation, ModelPinMergedObservation):
        after, at = _merged(row, observation), observation.merged_at
    elif isinstance(observation, ModelPinHeadObservation):
        after, at = _head(row, observation, config), observation.as_of
    elif isinstance(observation, ModelPinRunObservation):
        after, at = _run(row, observation), observation.as_of
    else:
        after, at = _check(row, observation, config), observation.completed_at
    if after is None or after == row:
        return FoldResult(row=row)
    after = after.model_copy(
        update={"last_event_at": max(at, row.last_event_at) if row else at}
    )
    return _flagged(after, at)


def _outcome(
    row: ModelPinFrictionRow | None, event: ModelPinOutcomeEvent
) -> ModelPinFrictionRow | None:
    opened = event.outcome in {EnumPinOutcomeKind.OPENED, EnumPinOutcomeKind.REPLAYED}
    refusals = (
        ()
        if opened
        else (
            ModelPinRefusal(
                refusal_class=event.refusal_class or EnumPinRefusalClass.OTHER,
                detail=event.detail,
                at=event.outcome_at,
            ),
        )
    )
    if row is None:
        return ModelPinFrictionRow(
            repository=event.repository,
            branch=event.branch,
            pin_kind=event.pin_kind,
            core_tag=event.core_tag,
            state=_state_of(event),
            first_seen_at=event.outcome_at,
            pr_number=event.pr_number,
            tag_pushed_at=event.tag_pushed_at,
            pr_opened_at=event.outcome_at if opened else None,
            seconds_tag_to_pr_opened=event.seconds_tag_to_pr_opened if opened else None,
            published_head_sha=event.branch_head_sha,
            head_shas=(event.branch_head_sha,) if event.branch_head_sha else (),
            refusals=refusals,
            last_event_at=event.outcome_at,
        )
    if row.state is EnumPinPrState.OPENED or row.state in _TERMINAL_STATES:
        # A redelivered tag replays an outcome already folded; a failure that
        # arrives after the pull request exists is a stale one.
        return row
    if opened:
        return row.model_copy(
            update={
                "state": EnumPinPrState.OPENED,
                "pr_number": event.pr_number,
                "pr_opened_at": event.outcome_at,
                "tag_pushed_at": event.tag_pushed_at or row.tag_pushed_at,
                "seconds_tag_to_pr_opened": event.seconds_tag_to_pr_opened,
                "published_head_sha": event.branch_head_sha,
                "head_shas": (event.branch_head_sha,) if event.branch_head_sha else (),
            }
        )
    known = {(r.refusal_class, r.detail) for r in row.refusals}
    fresh = tuple(r for r in refusals if (r.refusal_class, r.detail) not in known)
    return row.model_copy(update={"refusals": (*row.refusals, *fresh)})


def _state_of(event: ModelPinOutcomeEvent) -> EnumPinPrState:
    if event.outcome is EnumPinOutcomeKind.REFUSED:
        return EnumPinPrState.REFUSED
    if event.outcome is EnumPinOutcomeKind.FAILED:
        return EnumPinPrState.FAILED
    return EnumPinPrState.OPENED


def _merged(
    row: ModelPinFrictionRow, observation: ModelPinMergedObservation
) -> ModelPinFrictionRow:
    if row.state is EnumPinPrState.MERGED:
        return row
    opened = row.pr_opened_at
    return row.model_copy(
        update={
            "state": EnumPinPrState.MERGED,
            "pr_number": row.pr_number or observation.pr_number,
            "merged_at": observation.merged_at,
            "seconds_pr_open_to_merge": (
                None
                if opened is None
                else max((observation.merged_at - opened).total_seconds(), 0.0)
            ),
            "seconds_tag_to_merge": (
                None
                if row.tag_pushed_at is None
                else max(
                    (observation.merged_at - row.tag_pushed_at).total_seconds(), 0.0
                )
            ),
        }
    )


def _head(
    row: ModelPinFrictionRow,
    observation: ModelPinHeadObservation,
    config: ModelPinFrictionConfig,
) -> ModelPinFrictionRow:
    if row.state in _TERMINAL_STATES:
        return row
    update: dict[str, object] = {}
    if row.pr_number is None:
        update["pr_number"] = observation.pr_number
    if row.state is EnumPinPrState.OPENED:
        sha = observation.head_sha
        if sha and sha not in row.head_shas:
            update["head_shas"] = (*row.head_shas, sha)
        if observation.triage_state in config.closed_triage_states:
            update["state"] = EnumPinPrState.CLOSED
            update["closed_at"] = observation.as_of
        elif observation.mergeable == config.conflicting_mergeable:
            update["refusals"] = _with_refusal(
                row,
                EnumPinRefusalClass.CONFLICT,
                f"mergeable={observation.mergeable}",
                observation.as_of,
            )
    return row.model_copy(update=update) if update else row


def _run(
    row: ModelPinFrictionRow, observation: ModelPinRunObservation
) -> ModelPinFrictionRow:
    attempt = observation.run_attempt
    if (
        row.state is not EnumPinPrState.OPENED
        or observation.status != "requested"
        or attempt is None
        or attempt < 2
        or observation.head_sha not in row.head_shas
    ):
        return row
    key = f"{observation.run_id}:{attempt}"
    if key in row.rerun_keys:
        return row
    return row.model_copy(update={"rerun_keys": (*row.rerun_keys, key)})


def _check(
    row: ModelPinFrictionRow,
    observation: ModelPinCheckObservation,
    config: ModelPinFrictionConfig,
) -> ModelPinFrictionRow:
    if (
        row.state is not EnumPinPrState.OPENED
        or observation.conclusion not in config.red_conclusions
    ):
        return row
    for entry in config.check_refusal_classes:
        if re.search(entry.pattern, observation.check, re.IGNORECASE):
            refusals = _with_refusal(
                row,
                entry.refusal_class,
                f"{observation.check}: {observation.conclusion}",
                observation.completed_at,
            )
            return row.model_copy(update={"refusals": refusals})
    return row


def _with_refusal(
    row: ModelPinFrictionRow,
    refusal_class: EnumPinRefusalClass,
    detail: str,
    at: datetime,
) -> tuple[ModelPinRefusal, ...]:
    """The row's refusals plus this one, unless its class is already recorded."""
    if any(r.refusal_class is refusal_class for r in row.refusals):
        return row.refusals
    return (
        *row.refusals,
        ModelPinRefusal(refusal_class=refusal_class, detail=detail, at=at),
    )


def _flagged(after: ModelPinFrictionRow, at: datetime) -> FoldResult:
    """Record each friction reason once, and emit one event for each new reason."""
    reasons = list(after.friction_reasons)
    events: list[ModelPinFrictionEvent] = []

    def flag(reason: EnumPinFrictionReason, detail: str) -> None:
        if reason in reasons:
            return
        reasons.append(reason)
        events.append(
            ModelPinFrictionEvent(
                reason=reason,
                site=after.repository,
                pin_kind=after.pin_kind,
                core_tag=after.core_tag,
                branch=after.branch,
                pr_number=after.pr_number,
                fix_pushes=after.fix_pushes,
                ci_reruns=after.ci_reruns,
                refusal_classes=tuple(
                    dict.fromkeys(r.refusal_class for r in after.refusals)
                ),
                detail=detail,
                flagged_at=at,
            )
        )

    if after.state in _FAILED_STATES:
        latest = after.refusals[-1].detail if after.refusals else ""
        flag(EnumPinFrictionReason.UPDATER_FAILED, latest)
    if after.touches > 0:
        flag(
            EnumPinFrictionReason.HUMAN_TOUCHED,
            f"{after.fix_pushes} fix push(es) and {after.ci_reruns} CI re-run(s) "
            "after the updater",
        )
    return FoldResult(
        row=after.model_copy(update={"friction_reasons": tuple(reasons)}),
        friction=tuple(events),
    )


__all__ = ["FoldResult", "PinObservation", "fold_pin_observation"]
