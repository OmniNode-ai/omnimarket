# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab alarm's owner responder: one lane dispatch per uncovered cause key (OMN-24200).

A raised lab alarm that no live lane covers waits for a person to notice: from 2026-11-03 to
2026-11-13 the lab alarm wrote 255 FRICTION rows, every one citing the same umbrella ticket, and
the delegation chain canary was raised once on 2026-11-03T12:52:08Z. This handler reads those
rows and answers one lane dispatch per distinct cause key whose first raise is older than the
policy's bound and that no live lane covers. It is pure: no I/O, no clock, and every threshold
comes from the request's policy.

A lane covers a key only by naming the key (``covered_causes``) or by carrying the key's lane
stem (``covered_lanes``, the stall responder's rule). The ticket an alarm row cites in
``existing=`` never covers a key: it is the same umbrella ticket on every row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from omnimarket.handlers.ledger_row_grammar import parse_row
from omnimarket.models.model_stall_cause import (
    ModelCauseDispatch,
    ModelCauseSkip,
    ModelRowRefusal,
)
from omnimarket.nodes.node_alarm_owner_responder_compute.models.model_alarm_owner_responder import (
    ModelAlarmOwnerRequest,
    ModelAlarmOwnerResult,
)
from omnimarket.nodes.node_stall_responder_compute.handlers.handler_stall_cause_responder import (
    LANE_STAMP,
    lane_name,
    lane_stem,
    split_cause,
)

STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_NOT_SLUG = re.compile(r"[^a-z0-9]+")


def _slug(value: str) -> str:
    """Lower case, runs of characters outside [a-z0-9] to one hyphen, stripped of hyphens."""
    return _NOT_SLUG.sub("-", value.lower()).strip("-")


def _moment(stamp: str) -> datetime:
    return datetime.strptime(stamp, STAMP_FORMAT)


@dataclass
class _Raised:
    """The rows one cause key collected: its first raise and the ticket that row cites."""

    first: datetime
    first_stamp: str
    ticket: str
    occurrences: int = 0


def _covering_lane(key: str, lanes: tuple[str, ...]) -> str | None:
    """The live lane named `<stem>-<stamp>` for this key, matched whole (the stall responder's rule)."""
    stem = lane_stem(key)
    for lane in lanes:
        head, _, stamp = _slug(lane).rpartition("-")
        if head == stem and LANE_STAMP.fullmatch(stamp):
            return lane
    return None


class HandlerAlarmOwnerResponder:
    """Pure: lab alarm FRICTION rows in, one dispatch per uncovered cause key past its bound out."""

    def handle(self, request: ModelAlarmOwnerRequest) -> ModelAlarmOwnerResult:
        policy = request.policy
        now = _moment(request.now)
        conditions = frozenset(policy.conditions)
        refusals: list[ModelRowRefusal] = []
        raised: dict[str, _Raised] = {}
        alarm_rows = 0
        outside_conditions = 0

        for line in dict.fromkeys(
            line.strip() for line in request.rows if line.strip()
        ):
            row = parse_row(line, types=frozenset({"FRICTION"}))
            if row is None or row.value("actor") != policy.alarm_actor:
                continue
            try:
                stamp = _moment(row.stamp)
            except ValueError:
                refusals.append(
                    ModelRowRefusal(stamp=row.stamp, reason="invalid-calendar-date")
                )
                continue
            condition = row.value("condition")
            if not condition:
                refusals.append(
                    ModelRowRefusal(stamp=row.stamp, reason="missing-condition")
                )
                continue
            subject = row.value("subject")
            if not subject:
                refusals.append(
                    ModelRowRefusal(stamp=row.stamp, reason="missing-subject")
                )
                continue
            alarm_rows += 1
            if condition not in conditions:
                outside_conditions += 1
                continue
            key = f"{_slug(condition)}:{subject}"
            ticket = row.value("existing") or ""
            entry = raised.get(key)
            if entry is None:
                entry = raised[key] = _Raised(stamp, row.stamp, ticket)
            elif stamp < entry.first:
                entry.first, entry.first_stamp, entry.ticket = stamp, row.stamp, ticket
            entry.occurrences += 1

        bound = timedelta(hours=policy.owner_required_after_hours)
        window = timedelta(hours=policy.redispatch_after_hours)
        dispatched = dict(request.dispatched)
        dispatches: list[ModelCauseDispatch] = []
        skipped: list[ModelCauseSkip] = []
        for key, entry in raised.items():
            if now - entry.first < bound:
                skipped.append(
                    ModelCauseSkip(
                        cause_key=key,
                        reason="below-bound",
                        occurrences=entry.occurrences,
                    )
                )
                continue
            prior = request.dispatched.get(key)
            if prior is not None:
                try:
                    due = _moment(prior) + window
                except ValueError:
                    skipped.append(
                        ModelCauseSkip(
                            cause_key=key,
                            reason="invalid-dispatched-stamp",
                            occurrences=entry.occurrences,
                        )
                    )
                    continue
                if now < due:
                    skipped.append(
                        ModelCauseSkip(
                            cause_key=key,
                            reason="already-dispatched",
                            occurrences=entry.occurrences,
                        )
                    )
                    continue
            if key in request.covered_causes:
                skipped.append(
                    ModelCauseSkip(
                        cause_key=key,
                        reason="covered-by-cause",
                        occurrences=entry.occurrences,
                    )
                )
                continue
            covering = _covering_lane(key, request.covered_lanes)
            if covering is not None:
                skipped.append(
                    ModelCauseSkip(
                        cause_key=key,
                        reason=f"covered:{covering}",
                        occurrences=entry.occurrences,
                    )
                )
                continue
            cause_class, subject = split_cause(key)
            dispatched[key] = request.now
            dispatches.append(
                ModelCauseDispatch(
                    cause_key=key,
                    cause_class=cause_class,
                    subject=subject,
                    lane=lane_name(key, request.now),
                    ticket=entry.ticket,
                    first_stamp=entry.first_stamp,
                    occurrences=entry.occurrences,
                )
            )
        return ModelAlarmOwnerResult(
            dispatches=tuple(dispatches),
            skipped=tuple(skipped),
            dispatched=dispatched,
            alarm_rows=alarm_rows,
            outside_conditions=outside_conditions,
            refusals=tuple(refusals),
        )


__all__ = ["HandlerAlarmOwnerResponder"]
