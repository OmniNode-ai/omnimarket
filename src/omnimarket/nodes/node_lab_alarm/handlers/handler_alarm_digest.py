# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab alarm's daily digest (OMN-24939): one message per held day, and the fix-cause STATUS row.

The router (AlarmSeverityRouter) holds each new episode of a DIGEST source under its UTC day. This step, run once a
day by the verb ``alarm-digest``, posts one summary message for every held day before today that has not been
sent, marks the day sent and drops what it held; a day already sent is never sent again, and a post that fails
keeps the day for the next run. It also appends one STATUS row per day listing the FIX-CAUSE sources with the
cause the alarm review gave, under a request id derived from the day, so a rerun the same day appends nothing.
A probe (``write`` false) sends nothing and keeps nothing.
"""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid5

from omnimarket.nodes.node_lab_alarm.handlers.alarm_route import (
    DIGEST_PENDING,
    DIGEST_SENT,
)
from omnimarket.nodes.node_lab_alarm.models.model_alarm_severity import (
    EnumAlarmDigestOutcome,
    EnumAlarmSeverity,
    ModelAlarmDigestCommand,
    ModelAlarmDigestDay,
    ModelAlarmDigestResult,
    ModelAlarmSeverityTable,
)
from omnimarket.nodes.node_lab_alarm.protocols import ProtocolDigestStore
from omnimarket.nodes.node_lab_alarm_deliver_effect.protocols import (
    LedgerAppendError,
    ProtocolAlarmChannel,
    ProtocolAlarmLedger,
)

_DAY = "%Y-%m-%d"
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_FAILURE_LIMIT = 300
_SENT_KEPT_DAYS = 14
_ROW_BREAKERS = re.compile(r"\s+")


def _cell(value: str) -> str:
    return _ROW_BREAKERS.sub(" ", value.replace("|", "/")).strip()


def _clip(value: str, limit: int) -> str:
    flat = _cell(value)
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def render_digest_message(
    *, table: ModelAlarmSeverityTable, day: str, held: Mapping[str, Mapping[str, Any]]
) -> tuple[str, int, int]:
    """One day's digest text, its episode count and its source count."""
    routing = table.routing
    by_condition: dict[str, list[Mapping[str, Any]]] = {}
    for entry in held.values():
        by_condition.setdefault(str(entry["condition"]), []).append(entry)
    episodes = sum(int(e.get("episodes", 1)) for e in held.values())
    lines = [
        routing.digest_header_template.format(
            day=day, episodes=episodes, sources=len(by_condition)
        )
    ]
    ranked = sorted(
        by_condition.items(),
        key=lambda item: (-sum(int(e.get("episodes", 1)) for e in item[1]), item[0]),
    )
    for condition, entries in ranked[: routing.digest_line_limit]:
        latest = max(entries, key=lambda e: str(e.get("last_at", "")))
        try:
            review_name = table.source(condition).review_name
        except ValueError:
            review_name = "undeclared"
        lines.append(
            routing.digest_line_template.format(
                review_name=review_name,
                condition=condition,
                episodes=sum(int(e.get("episodes", 1)) for e in entries),
                subject=_clip(str(latest["subject"]), routing.digest_detail_limit),
                detail=_clip(str(latest["detail"]), routing.digest_detail_limit),
            )
        )
    rest = len(ranked) - routing.digest_line_limit
    if rest > 0:
        lines.append(f"- and {rest} more sources")
    return "\n".join(lines), episodes, len(by_condition)


def render_fix_cause_status_row(
    *,
    table: ModelAlarmSeverityTable,
    lane: str,
    actor: str,
    stamp: datetime,
    request_id: str,
) -> str:
    """One STATUS row naming every FIX-CAUSE source and its cause, for the orchestrator to put lanes on."""
    sources = table.of(EnumAlarmSeverity.FIX_CAUSE)
    listed = "; ".join(
        f"{s.condition} ({_cell(s.review_name)}): {_cell(s.cause)}" for s in sources
    )
    status = table.routing.fix_cause_status
    cells = [
        stamp.astimezone(UTC).strftime(_STAMP),
        "STATUS",
        f"lane={_cell(lane)}",
        f"actor={_cell(actor)}",
        "model=none",
        f"ticket={status.ticket}",
        f"kind={status.kind}",
        f"sources={len(sources)}",
        f"req={request_id}",
        "FIX-CAUSE alarm sources, routed as PAGE until their cause is fixed; each needs an owner lane on the cause: "
        + listed,
    ]
    return " | ".join(cells)


class HandlerAlarmDigest:
    """Send the held digest days and append the day's fix-cause STATUS row."""

    def __init__(
        self,
        *,
        table: ModelAlarmSeverityTable,
        channel: ProtocolAlarmChannel,
        ledger: ProtocolAlarmLedger,
        digest: ProtocolDigestStore,
        lane: str,
        actor: str,
        request_namespace: str,
        clock: Callable[[], datetime],
    ) -> None:
        self._table = table
        self._channel = channel
        self._ledger = ledger
        self._digest = digest
        self._lane = lane
        self._actor = actor
        self._namespace = UUID(request_namespace)
        self._clock = clock

    def handle(self, request: ModelAlarmDigestCommand) -> ModelAlarmDigestResult:
        now = self._clock().astimezone(UTC)
        today = now.strftime(_DAY)
        try:
            state: dict[str, Any] = copy.deepcopy(self._digest.load())
        except (OSError, ValueError) as exc:
            return ModelAlarmDigestResult(
                correlation_id=request.correlation_id,
                outcome=EnumAlarmDigestOutcome.FAILED,
                days=(),
                status_row="not-run",
                read_at=now,
                failure=f"digest-state: {str(exc)[:_FAILURE_LIMIT]}",
            )
        pending: dict[str, dict[str, Any]] = dict(state.get(DIGEST_PENDING, {}))
        sent: dict[str, str] = dict(state.get(DIGEST_SENT, {}))
        due = sorted(d for d in pending if d < today and d not in sent and pending[d])
        if not request.write:
            days = tuple(
                ModelAlarmDigestDay(
                    day=d,
                    episodes=sum(
                        int(e.get("episodes", 1)) for e in pending[d].values()
                    ),
                    sources=len({e["condition"] for e in pending[d].values()}),
                )
                for d in due
            )
            return ModelAlarmDigestResult(
                correlation_id=request.correlation_id,
                outcome=EnumAlarmDigestOutcome.SKIPPED,
                days=days,
                status_row="not-run",
                read_at=now,
            )
        results: list[ModelAlarmDigestDay] = []
        failures: list[str] = []
        for day in due:
            text, episodes, sources = render_digest_message(
                table=self._table, day=day, held=pending[day]
            )
            receipt = self._channel.send(
                category=self._table.routing.digest_category, text=text
            )
            if receipt.delivered:
                sent[day] = receipt.message_ref or "sent"
                del pending[day]
                results.append(
                    ModelAlarmDigestDay(
                        day=day,
                        episodes=episodes,
                        sources=sources,
                        message_ref=receipt.message_ref or "",
                    )
                )
            else:
                failure = f"{day}: digest channel: {(receipt.failure or 'refused')[:_FAILURE_LIMIT]}"
                failures.append(failure)
                results.append(
                    ModelAlarmDigestDay(
                        day=day, episodes=episodes, sources=sources, failure=failure
                    )
                )
        # Drop empty held days that are past, and sent marks older than the kept window.
        for day in [d for d, held in pending.items() if not held and d < today]:
            del pending[day]
        kept = sorted(sent)[-_SENT_KEPT_DAYS:]
        state[DIGEST_PENDING] = pending
        state[DIGEST_SENT] = {d: sent[d] for d in kept}
        try:
            self._digest.save(state)
        except (OSError, ValueError) as exc:
            failures.append(f"digest-state: {str(exc)[:_FAILURE_LIMIT]}")
        request_id = str(uuid5(self._namespace, f"alarm-fix-cause-status|{today}"))
        row = render_fix_cause_status_row(
            table=self._table,
            lane=self._lane,
            actor=self._actor,
            stamp=now,
            request_id=request_id,
        )
        try:
            status_row = self._ledger.append(request_id=request_id, row=row)
        except LedgerAppendError as exc:
            status_row = "failed"
            failures.append(f"status-row: {str(exc)[:_FAILURE_LIMIT]}")
        if failures:
            outcome = EnumAlarmDigestOutcome.FAILED
        elif any(r.message_ref for r in results):
            outcome = EnumAlarmDigestOutcome.SENT
        else:
            outcome = EnumAlarmDigestOutcome.NOTHING_DUE
        return ModelAlarmDigestResult(
            correlation_id=request.correlation_id,
            outcome=outcome,
            days=tuple(results),
            status_row=status_row,
            status_request_id=request_id,
            read_at=now,
            failure="; ".join(failures),
        )


__all__ = [
    "HandlerAlarmDigest",
    "render_digest_message",
    "render_fix_cause_status_row",
]
