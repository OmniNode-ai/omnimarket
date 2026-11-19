# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Apply declared lab alarm severity and deliver the daily digest."""

from __future__ import annotations

import copy
import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import yaml

from omnimarket.models.alarm_send_receipt import ModelAlarmSendReceipt
from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryOutcome,
    EnumDeliveryStep,
    ModelAlarmDelivery,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
    ModelLabAlarmDeliveryResult,
)
from omnimarket.nodes.node_lab_alarm.models.model_alarm_severity import (
    EnumAlarmDigestOutcome,
    EnumAlarmSeverity,
    ModelAlarmDigestCommand,
    ModelAlarmDigestDay,
    ModelAlarmDigestResult,
    ModelAlarmSeverityTable,
)
from omnimarket.nodes.node_lab_alarm.protocols import (
    ProtocolAlarmDelivery,
    ProtocolDigestStore,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.protocols import (
    LedgerAppendError,
    ProtocolAlarmChannel,
    ProtocolAlarmLedger,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

#: The digest store's keys: held episodes by UTC day, and the days already sent.
DIGEST_PENDING = "pending"
DIGEST_SENT = "sent"
_DAY = "%Y-%m-%d"
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_HELD_PREFIX = "held:"
_FAILURE_LIMIT = 300
_SENT_KEPT_DAYS = 14
_ROW_BREAKERS = re.compile(r"\s+")


def load_alarm_severity_table(path: Path = CONTRACT) -> ModelAlarmSeverityTable:
    """The declared severity table from the node's contract; refuses the load on any undeclared source.

    Every CLI operation of the node must be covered by a source naming it, so a condition added to the node
    without a severity is refused here, at load, before any delivery runs.
    """
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    if "alarm_sources" not in contract or "alarm_routing" not in contract:
        raise ValueError(f"{path} declares no alarm_sources or no alarm_routing")
    table = ModelAlarmSeverityTable.model_validate(
        {"sources": contract["alarm_sources"], "routing": contract["alarm_routing"]}
    )
    operations = {entry["operation"] for entry in contract.get("cli_handlers", [])}
    covered = {s.operation for s in table.sources if s.operation}
    uncovered = sorted(operations - covered)
    if uncovered:
        raise ValueError(
            f"node_lab_alarm operations with no declared alarm source severity: {uncovered}"
        )
    unknown = sorted(covered - operations)
    if unknown:
        raise ValueError(
            f"alarm sources name operations node_lab_alarm does not have: {unknown}"
        )
    return table


class GatedAlarmChannel:
    """The delivery's channel, which the router opens or closes per request; a closed gate holds the post."""

    def __init__(self, channel: ProtocolAlarmChannel) -> None:
        self._channel = channel
        self._mode = "post"
        self.held: list[tuple[str, str]] = []

    def set_mode(self, mode: str) -> None:
        self._mode = mode
        self.held = []

    def send(self, *, category: str, text: str) -> ModelAlarmSendReceipt:
        if self._mode == "post":
            return self._channel.send(category=category, text=text)
        self.held.append((category, text))
        return ModelAlarmSendReceipt(
            delivered=True, message_ref=f"{_HELD_PREFIX}{self._mode}"
        )


class HandlerAlarmRoute:
    """The delivery port with the declared severity applied; see the module docstring for each route."""

    def __init__(
        self,
        *,
        table: ModelAlarmSeverityTable,
        gate: GatedAlarmChannel,
        channel: ProtocolAlarmChannel,
        inner: ProtocolAlarmDelivery,
        digest: ProtocolDigestStore,
        clock: Callable[[], datetime],
    ) -> None:
        self._table = table
        self._gate = gate
        self._channel = channel
        self._inner = inner
        self._digest = digest
        self._clock = clock

    def handle(
        self, request: ModelLabAlarmDeliveryRequest
    ) -> ModelLabAlarmDeliveryResult:
        severity = self._table.source(request.condition).severity
        mode = {
            EnumAlarmSeverity.PAGE: "post",
            EnumAlarmSeverity.FIX_CAUSE: "post",
            EnumAlarmSeverity.DIGEST: "digest",
            EnumAlarmSeverity.DROP: "drop",
        }[severity]
        self._gate.set_mode(mode)
        try:
            result = self._inner.handle(request)
        finally:
            self._gate.set_mode("post")
        if not request.write:
            return result
        deliveries = tuple(self._rewrite(d) for d in result.deliveries)
        failures = [result.failure] if result.failure else []
        if severity is EnumAlarmSeverity.DIGEST:
            failures.extend(self._hold(request, result.deliveries))
        if severity in (EnumAlarmSeverity.PAGE, EnumAlarmSeverity.FIX_CAUSE):
            failures.extend(self._clear(request.condition, result.cleared))
        outcome = result.outcome
        if failures and outcome is not EnumDeliveryOutcome.FAILED:
            outcome = EnumDeliveryOutcome.FAILED
        return result.model_copy(
            update={
                "deliveries": deliveries,
                "outcome": outcome,
                "failure": "; ".join(failures),
            }
        )

    @staticmethod
    def _rewrite(delivery: ModelAlarmDelivery) -> ModelAlarmDelivery:
        """A held or dropped post is not a channel delivery: it reads SUPPRESSED, its ref naming the route."""
        if delivery.channel_ref.startswith(_HELD_PREFIX):
            return delivery.model_copy(update={"channel": EnumDeliveryStep.SUPPRESSED})
        return delivery

    def _hold(
        self,
        request: ModelLabAlarmDeliveryRequest,
        deliveries: tuple[ModelAlarmDelivery, ...],
    ) -> list[str]:
        """Record each new episode the delivery opened under today's UTC day for the daily digest."""
        opened = {
            d.key
            for d in deliveries
            if d.channel_ref == f"{_HELD_PREFIX}digest"
            and d.channel is EnumDeliveryStep.DELIVERED
        }
        alarms: dict[str, ModelLabAlarm] = {a.key: a for a in request.alarms}
        if not opened:
            return []
        now = self._clock().astimezone(UTC)
        try:
            state: dict[str, Any] = copy.deepcopy(self._digest.load())
            pending = state.setdefault(DIGEST_PENDING, {})
            day = pending.setdefault(now.strftime(_DAY), {})
            for key in sorted(opened):
                alarm = alarms[key]
                entry = day.get(key)
                if entry is None:
                    day[key] = {
                        "condition": alarm.condition,
                        "subject": alarm.subject,
                        "detail": alarm.detail,
                        "first_at": now.strftime(_STAMP),
                        "last_at": now.strftime(_STAMP),
                        "episodes": 1,
                    }
                else:
                    entry.update(
                        detail=alarm.detail,
                        last_at=now.strftime(_STAMP),
                        episodes=int(entry.get("episodes", 1)) + 1,
                    )
            state.setdefault(DIGEST_SENT, {})
            self._digest.save(state)
        except (OSError, ValueError) as exc:
            return [f"digest-hold: {str(exc)[:_FAILURE_LIMIT]}"]
        return []

    def _clear(self, condition: str, cleared: tuple[str, ...]) -> list[str]:
        """One clear message per episode the condition's OK ended."""
        failures: list[str] = []
        prefix = f"{condition}:"
        routing = self._table.routing
        for key in cleared:
            subject = key[len(prefix) :] if key.startswith(prefix) else key
            receipt = self._channel.send(
                category=routing.clear_category,
                text=routing.clear_template.format(
                    condition=condition, subject=subject
                ),
            )
            if not receipt.delivered:
                failures.append(
                    f"{key}: clear channel: {(receipt.failure or 'refused')[:_FAILURE_LIMIT]}"
                )
        return failures


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
    "CONTRACT",
    "DIGEST_PENDING",
    "DIGEST_SENT",
    "GatedAlarmChannel",
    "HandlerAlarmDigest",
    "HandlerAlarmRoute",
    "load_alarm_severity_table",
    "render_digest_message",
    "render_fix_cause_status_row",
]
