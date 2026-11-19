# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Route every lab alarm delivery by its source's declared severity (OMN-24939).

The router is the delivery port node_lab_alarm's conditions hand their verdicts to. It wraps the existing delivery
(node_lab_alarm_deliver_effect's HandlerLabAlarmDeliver) and changes only where that delivery's channel posts go;
the episode rule (a subject posts when it ENTERS the alarm state and not again until its condition reads OK), the
ledger row and its dedupe window, and the owner route stay the delivery's own. There is no second dedupe layer:

- PAGE and FIX-CAUSE: the delivery posts as it always has (once per new condition); when the condition's OK ends
  an episode the router posts one clear message for each episode key the delivery reports cleared.
- DIGEST: the delivery's post for a new condition is held, and the router records the episode under its UTC day
  for the daily digest step (HandlerAlarmDigest), which sends one message per day.
- DROP: the delivery's post is discarded; the ledger row is still written by the delivery.

A request whose condition the table does not declare is refused with UndeclaredAlarmSourceError (a ValueError,
which the conditions report as delivery-failed) before anything is posted or kept.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

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
    EnumAlarmSeverity,
    ModelAlarmSeverityTable,
)
from omnimarket.nodes.node_lab_alarm.protocols import (
    ProtocolAlarmDelivery,
    ProtocolDigestStore,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.protocols import (
    ProtocolAlarmChannel,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

#: The digest store's keys: held episodes by UTC day, and the days already sent.
DIGEST_PENDING = "pending"
DIGEST_SENT = "sent"
_DAY = "%Y-%m-%d"
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_HELD_PREFIX = "held:"
_FAILURE_LIMIT = 300


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


class AlarmSeverityRouter:
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


__all__ = [
    "CONTRACT",
    "DIGEST_PENDING",
    "DIGEST_SENT",
    "AlarmSeverityRouter",
    "GatedAlarmChannel",
    "load_alarm_severity_table",
]
