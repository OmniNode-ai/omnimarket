# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Raise delivery health signals through the existing alarm's durable and alert seams."""

from omnimarket.models.error_rule_delivery import (
    ModelDeliveryAlarmRequest,
    ModelDeliveryAlarmResult,
)

from ..models import ModelSilentFailureAlarmOptions
from .alarm_io import append_row, one_line
from .handler_silent_failure_alarm import HandlerSilentFailureAlarm


class HandlerDeliveryHookAlarm:
    def __init__(self, options: ModelSilentFailureAlarmOptions | None = None) -> None:
        self.options = options or ModelSilentFailureAlarmOptions.from_environment()
        self.alarm = HandlerSilentFailureAlarm(self.options)

    def handle(self, request: ModelDeliveryAlarmRequest) -> ModelDeliveryAlarmResult:
        options = self.options
        ledger = (
            options.ledger
            or options.registry_root / "docs/tracking/ROLLING_WORK_LEDGER.md"
        )
        rows = []
        warnings = []
        for signal in request.signals:
            detail = one_line(
                signal.detail, "delivery path has no matcher receipt"
            ).replace("|", "/")
            row = (
                f"{request.now.strftime('%Y-%m-%dT%H:%M:%SZ')} | FRICTION | "
                "lane=silent-failure-alarm | ticket=OMN-19998 | actor=delivery-probe | "
                f"class=error-rule-delivery | surface={signal.surface} | cause={signal.cause} | "
                f"cost=est ~1 lane-minute investigating delivery silence | {detail}"
            )
            rows.append(row)
            if request.apply:
                appended, error = append_row(
                    options.python,
                    options.ledger_lock,
                    ledger,
                    row,
                    registry_root=options.registry_root,
                )
                if not appended:
                    warnings.append(f"delivery alarm ledger append failed: {error}")
                delivered, error, _ = self.alarm.deliver("error_rule_delivery", row)
                if not delivered:
                    warnings.append(f"delivery alarm alert failed: {error}")
        return ModelDeliveryAlarmResult(
            raised=bool(rows) and not warnings,
            rows=tuple(rows),
            warnings=tuple(warnings),
        )
