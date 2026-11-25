# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Run the delegation checks a change selects and alarm for each that turned red.

A change (a settings-store version, a host model server, a release) selects checks through the settings' key-to-check
rules. Each selected check runs after the change applied; its reading is compared with the check's previous reading, and a
check that read red where it did not read red before alarms once, to the owner its setting names, through the lab alarm
delivery. A check that stays green, or was red already, raises nothing. A prod target is refused before anything runs.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    EnumDeliveryOutcome,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_delegation_check_regression_effect.handlers.selection import (
    changed_keys,
    select_checks,
)
from omnimarket.nodes.node_delegation_check_regression_effect.models.model_check_regression import (
    EnumCheckRegressionOutcome,
    EnumCheckVerdict,
    ModelCheckAlarm,
    ModelCheckRegressionRequest,
    ModelCheckRegressionResult,
    ModelCheckVerdict,
)
from omnimarket.nodes.node_delegation_check_regression_effect.overlay import (
    ModelCheckRegressionSettings,
)
from omnimarket.nodes.node_delegation_check_regression_effect.protocols import (
    ProtocolAlarmDelivery,
    ProtocolCheckHistory,
    ProtocolCheckRunner,
)


class HandlerCheckRegression:
    def __init__(
        self,
        *,
        settings: ModelCheckRegressionSettings,
        runner: ProtocolCheckRunner,
        history: ProtocolCheckHistory,
        delivery: ProtocolAlarmDelivery | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._settings = settings
        self._runner = runner
        self._history = history
        self._delivery = delivery
        self._clock = clock

    def handle(
        self, request: ModelCheckRegressionRequest
    ) -> ModelCheckRegressionResult:
        refused = {t.lower() for t in self._settings.refused_targets}
        if request.target.lower() in refused:
            return ModelCheckRegressionResult(
                correlation_id=request.correlation_id,
                outcome=EnumCheckRegressionOutcome.REFUSED,
                failure=f"target {request.target!r} is refused: no check runs and no alarm goes there",
            )
        keys = tuple(
            sorted(
                set(request.changed_keys)
                | set(changed_keys(request.previous_document, request.new_document))
            )
        )
        selected = select_checks(
            self._settings, request.change_kind, request.subject, keys
        )
        if not selected:
            return ModelCheckRegressionResult(
                correlation_id=request.correlation_id,
                outcome=EnumCheckRegressionOutcome.NO_CHECKS,
                changed_keys=keys,
            )
        verdicts: list[ModelCheckVerdict] = []
        alarms: list[ModelCheckAlarm] = []
        for check_id in selected:
            check = self._settings.checks[check_id]
            before = self._history.previous(check_id, before=request.applied_at)
            reading = self._runner.run(
                check_id,
                since=request.applied_at,
                timeout_seconds=check.read_deadline_seconds,
            )
            previous = before.verdict if before else None
            verdict = reading.verdict if reading else EnumCheckVerdict.UNREAD
            verdicts.append(
                ModelCheckVerdict(
                    check_id=check_id,
                    verdict=verdict,
                    previous=previous,
                    run_ref=reading.run_ref if reading else "",
                )
            )
            if verdict is EnumCheckVerdict.RED and previous is not EnumCheckVerdict.RED:
                after = self._clock() - request.applied_at
                alarms.append(
                    ModelCheckAlarm(
                        check_id=check_id,
                        owner=check.owner,
                        previous=previous,
                        current=verdict,
                        run_ref=reading.run_ref if reading else "",
                        detail=self._detail(
                            request,
                            check_id,
                            check.owner,
                            keys,
                            previous,
                            reading.run_ref if reading else "",
                            after,
                        ),
                        raised_after=after,
                        late=after
                        > timedelta(seconds=self._settings.alarm_deadline_seconds),
                    )
                )
        outcome = (
            EnumCheckRegressionOutcome.ALARM
            if alarms
            else EnumCheckRegressionOutcome.OK
        )
        result = ModelCheckRegressionResult(
            correlation_id=request.correlation_id,
            outcome=outcome,
            changed_keys=keys,
            selected_checks=selected,
            verdicts=tuple(verdicts),
            alarms=tuple(alarms),
        )
        return self._deliver(request, result)

    def _detail(
        self,
        request: ModelCheckRegressionRequest,
        check_id: str,
        owner: str,
        keys: tuple[str, ...],
        previous: EnumCheckVerdict | None,
        run_ref: str,
        after: timedelta,
    ) -> str:
        shown = ", ".join(keys[:3]) + (" ..." if len(keys) > 3 else "")
        run = f" run {run_ref}" if run_ref else ""
        return (
            f"{check_id} read red{run} after a {request.change_kind.value} change of {request.subject} "
            f"({shown or 'no key named'}) applied {request.applied_at.isoformat()}; previous reading "
            f"{previous.value if previous else 'none'}; alarm {after.total_seconds():.0f}s after the change; owner: {owner}"
        )

    def _deliver(
        self,
        request: ModelCheckRegressionRequest,
        result: ModelCheckRegressionResult,
    ) -> ModelCheckRegressionResult:
        if self._delivery is None or not request.write:
            return result
        condition = self._settings.condition
        try:
            reply = self._delivery.handle(
                ModelLabAlarmDeliveryRequest(
                    correlation_id=request.correlation_id,
                    condition=condition,
                    outcome=EnumDeliveryCondition.ALARM
                    if result.alarms
                    else EnumDeliveryCondition.OK,
                    alarms=tuple(
                        ModelLabAlarm(
                            condition=condition,
                            subject=f"{a.check_id}:{request.change_kind.value}:{request.subject}",
                            detail=a.detail,
                        )
                        for a in result.alarms
                    ),
                    evidence=f"{len(result.selected_checks)} checks run after the change: "
                    + ", ".join(
                        f"{v.check_id}={v.verdict.value}" for v in result.verdicts
                    ),
                )
            )
        except (ValueError, OSError) as exc:
            return result.model_copy(
                update={"failure": f"delivery-unavailable:{type(exc).__name__}: {exc}"}
            )
        entries = tuple(
            f"{d.key} channel={d.channel.value} ledger={d.ledger.value}"
            for d in reply.deliveries
        )
        failure = (
            f"delivery-failed:{reply.failure}"
            if reply.outcome is EnumDeliveryOutcome.FAILED
            else ""
        )
        return result.model_copy(update={"delivery": entries, "failure": failure})


__all__ = ["HandlerCheckRegression"]
