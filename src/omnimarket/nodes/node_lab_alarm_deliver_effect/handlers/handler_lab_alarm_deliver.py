# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The lab alarm's delivery: one post to the alert channel and one ledger row per episode (OMN-23906).

Edge-triggered, as scripts/lab_alarm.py is: a subject is delivered when it ENTERS the alarm state and not
again until its condition reads OK. An INDETERMINATE read neither clears nor starts an episode ("I could not
tell" is not "it recovered"). The channel step is once per episode and a failure is a typed failure of the
run; the ledger step is retried on every later request until the row lands, and a subject that clears and
re-raises inside the overlay's window appends no second row. A condition the overlay routes to an owner
also gets, once per episode under the same window, one push notice on the alert channel and one MSG row
addressed to the owner lane, retried the same way. A request that does not write delivers nothing
and keeps nothing.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

import yaml
from pydantic import ValidationError

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    EnumDeliveryStep,
    ModelAlarmDelivery,
    ModelLabAlarmDeliveryRequest,
    ModelLabAlarmDeliveryResult,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.overlay import (
    ModelDeliveryOverlay,
    ModelLedgerSettings,
    ModelOwnerRoute,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.protocols import (
    LedgerAppendError,
    ProtocolAlarmChannel,
    ProtocolAlarmLedger,
    ProtocolDeliveryStateStore,
)

CONTRACT = Path(__file__).resolve().parents[1] / "contract.yaml"

_ACTIVE = "active"
_LAST_ROW = "ledger_last_row"
_PENDING = "ledger_pending"
_OWNER_PENDING = "owner_pending"
_OWNER_LAST = "owner_last"
_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_FAILURE_LIMIT = 300
_ROW_BREAKERS = re.compile(r"\s+")


def load_topics(path: Path = CONTRACT) -> dict[str, str]:
    """Topics by role from the node's contract."""
    contract = yaml.safe_load(path.read_text(encoding="utf-8"))
    return dict(contract["topics"])


def _cell(value: str, *, limit: int | None = None) -> str:
    """One cell's text: no pipe, no newline, so it cannot become another cell."""
    flattened = _ROW_BREAKERS.sub(" ", value.replace("|", "/")).strip()
    if limit is not None and len(flattened) > limit:
        return flattened[: limit - 1] + "…"
    return flattened


def render_ledger_row(
    *,
    condition: str,
    subject: str,
    detail: str,
    evidence: str,
    stamp: datetime,
    raised_at: str,
    request_id: str,
    settings: ModelLedgerSettings,
) -> str:
    """One FRICTION row naming the condition, the subject and the evidence the condition read."""
    cells = [
        stamp.astimezone(UTC).strftime(_STAMP),
        "FRICTION",
        f"lane={settings.lane}",
        f"actor={settings.actor}",
        "model=none",
        f"class={settings.friction_class}",
        f"condition={_cell(condition)}",
        f"subject={_cell(subject)}",
        f"raised_at={raised_at}",
        f"existing={settings.ticket}",
        f"cost={_cell(settings.cost)}",
        f"detail={_cell(detail)}",
        f"evidence={_cell(evidence, limit=settings.evidence_limit)}",
        f"req={request_id}",
        f"lab alarm raised {_cell(condition)} on {_cell(subject)}; "
        "the detail and evidence cells carry what it read",
    ]
    return " | ".join(cells)


def render_owner_row(
    *,
    route: ModelOwnerRoute,
    lane: str,
    text: str,
    stamp: datetime,
    request_id: str,
) -> str:
    """One MSG row addressed to the route's owner lane, carrying the route's ticket and the request id."""
    stamped = stamp.astimezone(UTC).strftime(_STAMP)
    from_lane = _cell(lane)
    ident = f"{stamped}-{from_lane}"
    cells = [
        stamped,
        "MSG",
        f"from={from_lane}",
        f"to={_cell(route.lane)}",
        f"id={_cell(ident)}",
        f"ticket={_cell(route.ticket)}",
        f"req={_cell(request_id)}",
        _cell(text),
    ]
    return " | ".join(cells)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime(_STAMP)


class HandlerLabAlarmDeliver:
    """Deliver the alarms one condition read, over the injected channel, ledger and episode state."""

    def __init__(
        self,
        *,
        overlay: ModelDeliveryOverlay,
        channel: ProtocolAlarmChannel,
        ledger: ProtocolAlarmLedger,
        state: ProtocolDeliveryStateStore,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._channel = channel
        self._ledger = ledger
        self._state = state
        self._clock = clock

    def handle(
        self, request: ModelLabAlarmDeliveryRequest
    ) -> ModelLabAlarmDeliveryResult:
        if not request.write:
            return ModelLabAlarmDeliveryResult(
                correlation_id=request.correlation_id,
                condition=request.condition,
                outcome=EnumDeliveryOutcome.SKIPPED,
                deliveries=(),
                cleared=(),
                retried=(),
                state_kept=False,
            )
        now = self._clock()
        loaded = self._state.load()
        active: dict[str, str] = dict(loaded.get(_ACTIVE, {}))
        window = timedelta(minutes=self._overlay.ledger.dedupe_window_minutes)
        last_row: dict[str, str] = {
            key: stamped
            for key, stamped in dict(loaded.get(_LAST_ROW, {})).items()
            if now - datetime.strptime(stamped, _STAMP).replace(tzinfo=UTC) < window
        }
        pending: dict[str, dict[str, str]] = {
            key: dict(entry) for key, entry in dict(loaded.get(_PENDING, {})).items()
        }
        owner_last: dict[str, str] = {
            key: stamped
            for key, stamped in dict(loaded.get(_OWNER_LAST, {})).items()
            if now - datetime.strptime(stamped, _STAMP).replace(tzinfo=UTC) < window
        }
        owner_pending: dict[str, dict[str, str]] = {
            key: dict(entry)
            for key, entry in dict(loaded.get(_OWNER_PENDING, {})).items()
        }
        prefix = f"{request.condition}:"
        steps: dict[str, ModelAlarmDelivery] = {}
        cleared: list[str] = []
        if request.outcome is EnumDeliveryCondition.OK:
            for key in tuple(active):
                if not key.startswith(prefix):
                    continue
                if request.recovery_detail:
                    channel, channel_ref, failure = self._send(
                        f"Recovered: {request.recovery_detail}"
                    )
                    steps[key] = ModelAlarmDelivery(
                        key=key,
                        channel=channel,
                        channel_ref=channel_ref,
                        ledger=EnumDeliveryStep.NOT_DUE,
                        failure=failure,
                    )
                    if failure:
                        continue
                del active[key]
                cleared.append(key)

        work: dict[str, dict[str, str]] = dict(pending)
        owner_work: dict[str, dict[str, str]] = dict(owner_pending)
        retried = tuple(pending)
        if request.outcome is EnumDeliveryCondition.ALARM:
            for alarm in request.alarms:
                key = alarm.key
                if key in active:
                    steps[key] = ModelAlarmDelivery(
                        key=key,
                        channel=EnumDeliveryStep.SUPPRESSED,
                        ledger=EnumDeliveryStep.NOT_DUE,
                    )
                    continue
                active[key] = _iso(now)
                channel, channel_ref, failure = self._post(alarm)
                if key in pending:
                    ledger = EnumDeliveryStep.NOT_DUE
                elif key in last_row:
                    ledger = EnumDeliveryStep.SUPPRESSED
                else:
                    ledger = EnumDeliveryStep.NOT_DUE
                    work[key] = {
                        "condition": alarm.condition,
                        "subject": alarm.subject,
                        "detail": alarm.detail,
                        "evidence": request.evidence or "no evidence recorded",
                        "raised_at": _iso(now),
                    }
                owner = EnumDeliveryStep.NOT_DUE
                if (
                    alarm.condition in self._overlay.owner_routes
                    and key not in owner_pending
                ):
                    # A pending entry keeps its raised_at and its pushed flag; the owner step
                    # below retries it. A stamp in the window suppresses the whole owner step.
                    if key in owner_last:
                        owner = EnumDeliveryStep.SUPPRESSED
                    else:
                        owner_work[key] = {
                            "condition": alarm.condition,
                            "subject": alarm.subject,
                            "detail": alarm.detail,
                            "evidence": request.evidence or "no evidence recorded",
                            "raised_at": _iso(now),
                            "pushed": "no",
                        }
                steps[key] = ModelAlarmDelivery(
                    key=key,
                    channel=channel,
                    channel_ref=channel_ref,
                    ledger=ledger,
                    owner=owner,
                    failure=failure,
                )

        for key, entry in work.items():
            step, request_id, failure = self._append(key, entry, now)
            if step is EnumDeliveryStep.FAILED:
                pending[key] = entry
            else:
                pending.pop(key, None)
                last_row[key] = _iso(now)
            prior = steps.get(
                key,
                ModelAlarmDelivery(
                    key=key,
                    channel=EnumDeliveryStep.NOT_DUE,
                    ledger=EnumDeliveryStep.NOT_DUE,
                ),
            )
            steps[key] = prior.model_copy(
                update={
                    "ledger": step,
                    "ledger_request_id": request_id,
                    "failure": "; ".join(
                        part for part in (prior.failure, failure) if part
                    ),
                }
            )

        for key, entry in owner_work.items():
            step, request_id, failure = self._deliver_owner(key, entry, now)
            if step is EnumDeliveryStep.FAILED:
                owner_pending[key] = entry
            else:
                owner_pending.pop(key, None)
                owner_last[key] = _iso(now)
            prior = steps.get(
                key,
                ModelAlarmDelivery(
                    key=key,
                    channel=EnumDeliveryStep.NOT_DUE,
                    ledger=EnumDeliveryStep.NOT_DUE,
                ),
            )
            steps[key] = prior.model_copy(
                update={
                    "owner": step,
                    "owner_request_id": request_id,
                    "failure": "; ".join(
                        part for part in (prior.failure, failure) if part
                    ),
                }
            )

        saved: dict[str, Any] = {
            _ACTIVE: active,
            _LAST_ROW: last_row,
            _PENDING: pending,
        }
        if owner_pending:
            saved[_OWNER_PENDING] = owner_pending
        if owner_last:
            saved[_OWNER_LAST] = owner_last
        self._state.save(saved)
        deliveries = tuple(steps.values())
        failed = [d for d in deliveries if d.failure]
        ran = any(
            EnumDeliveryStep.DELIVERED in (d.channel, d.ledger, d.owner)
            or EnumDeliveryStep.DUPLICATE in (d.ledger, d.owner)
            for d in deliveries
        )
        outcome = (
            EnumDeliveryOutcome.FAILED
            if failed
            else EnumDeliveryOutcome.DELIVERED
            if ran
            else EnumDeliveryOutcome.NOTHING_NEW
        )
        return ModelLabAlarmDeliveryResult(
            correlation_id=request.correlation_id,
            condition=request.condition,
            outcome=outcome,
            deliveries=deliveries,
            cleared=tuple(cleared),
            retried=retried,
            state_kept=True,
            failure="; ".join(f"{d.key}: {d.failure}" for d in failed),
        )

    def _post(self, alarm: Any) -> tuple[EnumDeliveryStep, str, str]:
        settings = self._overlay.channel
        return self._send(
            settings.message_template.format(
                condition=alarm.condition, subject=alarm.subject, detail=alarm.detail
            )
        )

    def _send(self, text: str) -> tuple[EnumDeliveryStep, str, str]:
        receipt = self._channel.send(category=self._overlay.channel.category, text=text)
        if receipt.delivered:
            return EnumDeliveryStep.DELIVERED, receipt.message_ref or "", ""
        return (
            EnumDeliveryStep.FAILED,
            "",
            f"channel: {(receipt.failure or 'refused')[:_FAILURE_LIMIT]}",
        )

    def _append(
        self, key: str, entry: Mapping[str, str], now: datetime
    ) -> tuple[EnumDeliveryStep, str, str]:
        settings = self._overlay.ledger
        request_id = str(
            uuid5(UUID(settings.request_namespace), f"{key}|{entry['raised_at']}")
        )
        row = render_ledger_row(
            condition=entry["condition"],
            subject=entry["subject"],
            detail=entry["detail"],
            evidence=entry["evidence"],
            stamp=now,
            raised_at=entry["raised_at"],
            request_id=request_id,
            settings=settings,
        )
        try:
            answer = self._ledger.append(request_id=request_id, row=row)
        except LedgerAppendError as exc:
            return (
                EnumDeliveryStep.FAILED,
                request_id,
                f"ledger: {str(exc)[:_FAILURE_LIMIT]}",
            )
        step = (
            EnumDeliveryStep.DUPLICATE
            if answer == "duplicate"
            else EnumDeliveryStep.DELIVERED
        )
        return step, request_id, ""

    def _deliver_owner(
        self, key: str, entry: dict[str, str], now: datetime
    ) -> tuple[EnumDeliveryStep, str, str]:
        """The owner step: one push notice, then one MSG row, each retried until it lands."""
        settings = self._overlay.ledger
        route = self._overlay.owner_routes.get(entry["condition"])
        if route is None:
            # The overlay no longer routes this condition: a kept entry has nothing left to deliver.
            return EnumDeliveryStep.NOT_DUE, "", ""
        request_id = str(
            uuid5(UUID(settings.request_namespace), f"owner|{key}|{entry['raised_at']}")
        )
        evidence = _cell(entry["evidence"], limit=settings.evidence_limit)
        filled = {
            "condition": entry["condition"],
            "subject": entry["subject"],
            "detail": entry["detail"],
            "evidence": evidence,
        }
        failures: list[str] = []
        if entry.get("pushed", "no") == "no":
            receipt = self._channel.send(
                category=route.push_category,
                text=route.push_template.format(**filled),
            )
            if receipt.delivered:
                entry["pushed"] = "yes"
            else:
                failures.append(
                    f"channel: {(receipt.failure or 'refused')[:_FAILURE_LIMIT]}"
                )
        row = render_owner_row(
            route=route,
            lane=settings.lane,
            text=route.text_template.format(**filled),
            stamp=now,
            request_id=request_id,
        )
        try:
            answer = self._ledger.append(request_id=request_id, row=row)
        except LedgerAppendError as exc:
            failures.append(f"ledger: {str(exc)[:_FAILURE_LIMIT]}")
            return EnumDeliveryStep.FAILED, request_id, "; ".join(failures)
        if failures:
            return EnumDeliveryStep.FAILED, request_id, "; ".join(failures)
        step = (
            EnumDeliveryStep.DUPLICATE
            if answer == "duplicate"
            else EnumDeliveryStep.DELIVERED
        )
        return step, request_id, ""


def events_for(
    result: ModelLabAlarmDeliveryResult, topics: Mapping[str, str]
) -> list[tuple[str, dict[str, object]]]:
    """The chain of one run: started, one event per subject, then the completed or the failed terminal."""
    cid = result.correlation_id
    chain: list[tuple[str, dict[str, object]]] = [
        (
            topics["delivery_started"],
            {"correlation_id": cid, "condition": result.condition},
        )
    ]
    chain.extend(
        (
            topics["delivery_alarm"],
            {
                "correlation_id": cid,
                "key": d.key,
                "channel": d.channel.value,
                "ledger": d.ledger.value,
                "owner": d.owner.value,
            },
        )
        for d in result.deliveries
    )
    failed = result.outcome is EnumDeliveryOutcome.FAILED
    chain.append(
        (
            topics["delivery_failed" if failed else "delivery_completed"],
            {
                "correlation_id": cid,
                "condition": result.condition,
                "outcome": result.outcome.value,
                "cleared": list(result.cleared),
                "retried": list(result.retried),
                "failure": result.failure,
            },
        )
    )
    return chain


def parse_command(body: bytes) -> ModelLabAlarmDeliveryRequest | str:
    """The request in a body, or the bad-payload failure naming why."""
    try:
        return ModelLabAlarmDeliveryRequest.model_validate_json(body)
    except ValidationError as exc:
        return "bad-payload:" + ",".join(
            ".".join(str(p) for p in e["loc"]) or "body" for e in exc.errors()
        )


__all__ = [
    "HandlerLabAlarmDeliver",
    "events_for",
    "load_topics",
    "parse_command",
    "render_ledger_row",
    "render_owner_row",
]
