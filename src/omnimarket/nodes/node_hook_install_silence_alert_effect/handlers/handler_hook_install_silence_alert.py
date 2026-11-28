# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Feed the install-silence judge and hand its alert text to the lab alarm effect (OMN-26658, AC4).

Reads events per host, actor and time from the claude_hook_events projection and each install's last session from
the ledger lane rows (CLAIM rows naming `host=` and an `actor=` that starts with the tool), judges with
HandlerHookInstallEmission, and delivers through the existing alarm effect. An unreadable projection or ledger is an
alarm of its own, never a quiet pass. Every reader and the alarm are injected.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from omnimarket.models.lab_alarm_delivery import (
    EnumDeliveryCondition,
    ModelLabAlarm,
    ModelLabAlarmDeliveryRequest,
)
from omnimarket.nodes.node_hook_install_emission_compute.handlers import (
    HandlerHookInstallEmission,
)
from omnimarket.nodes.node_hook_install_emission_compute.models import (
    ModelHookInstallDeclared,
    ModelHookInstallEmissionRequest,
)
from omnimarket.nodes.node_hook_install_silence_alert_effect.handlers.postgres_events_reader import (
    DSN_ENV,
    PostgresHookInstallEventsReader,
)
from omnimarket.nodes.node_hook_install_silence_alert_effect.models import (
    EnumSilenceOutcome,
    ModelHookInstallSilenceResult,
    ModelHookInstallSilenceSettings,
    ModelHookInstallSilenceTrigger,
    ModelOwnedInstall,
)
from omnimarket.nodes.node_hook_install_silence_alert_effect.overlay import (
    load_settings,
)
from omnimarket.nodes.node_hook_install_silence_alert_effect.protocols import (
    ProtocolHookInstallAlarm,
    ProtocolHookInstallEventsReader,
)
from omnimarket.nodes.node_lab_alarm_deliver_effect.handlers.handler_lab_alarm_deliver_route import (
    HandlerLabAlarmDeliverRoute,
)
from omnimarket.nodes.node_work_progress_alert_effect.handlers.gather_lanes import (
    parse_cells,
)
from omnimarket.nodes.node_work_progress_alert_effect.handlers.ledger_row_reader import (
    PostgresLedgerRowReader,
    resolve_dsn,
)
from omnimarket.nodes.node_work_progress_alert_effect.handlers.protocol_ledger_row_reader import (
    ProtocolLedgerRowReader,
)
from omnimarket.nodes.node_work_progress_alert_effect.models import ModelLedgerRow

logger = logging.getLogger(__name__)
CONDITION = "hook_install_silent"
_PROJECTION = "claude_hook_events"


def _names(install: ModelOwnedInstall) -> frozenset[str]:
    return frozenset(n.casefold() for n in (install.host, *install.host_aliases))


class HandlerHookInstallSilenceAlert:
    def __init__(
        self,
        *,
        settings: ModelHookInstallSilenceSettings | None = None,
        events_reader: ProtocolHookInstallEventsReader | None = None,
        ledger_reader: ProtocolLedgerRowReader | None = None,
        alarm: ProtocolHookInstallAlarm | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        # Left None, each is resolved on the first request (overlay, DSN), never while the runtime wires the node.
        self._settings_in = settings
        self._events_in = events_reader
        self._ledger_in = ledger_reader
        self._alarm_in = alarm
        self._clock = clock or (lambda: datetime.now(UTC))
        self._judge = HandlerHookInstallEmission()
        self._last_run: datetime | None = None
        self._settings: ModelHookInstallSilenceSettings
        self._events: ProtocolHookInstallEventsReader
        self._ledger: ProtocolLedgerRowReader
        self._alarm: ProtocolHookInstallAlarm

    def handle(
        self, request: ModelHookInstallSilenceTrigger
    ) -> ModelHookInstallSilenceResult:
        try:
            self._resolve()
        except Exception as exc:
            reason = f"not configured: {type(exc).__name__}: {exc}"
            logger.error("Hook install silence refused: %s", reason)
            return ModelHookInstallSilenceResult(
                outcome=EnumSilenceOutcome.UNREADABLE, reason=reason, alert_text=reason
            )
        now = self._clock()
        interval = timedelta(seconds=self._settings.check_interval_seconds)
        if (
            self._last_run is not None
            and timedelta(0) <= now - self._last_run < interval
        ):
            return ModelHookInstallSilenceResult(outcome=EnumSilenceOutcome.THROTTLED)
        self._last_run = now
        correlation = str(request.correlation_id or uuid4())
        since = now - timedelta(minutes=self._settings.lookback_minutes)
        try:
            events = tuple(self._events.read(since))
        except Exception as exc:
            return self._unreadable(correlation, _PROJECTION, exc)
        try:
            rows = tuple(self._ledger.read(since))
        except Exception as exc:
            return self._unreadable(correlation, "work_ledger_rows", exc)

        sessions = self._last_sessions(rows)
        verdict = self._judge.handle(
            ModelHookInstallEmissionRequest(
                installs=tuple(
                    ModelHookInstallDeclared(
                        host=i.host,
                        tool=i.tool,
                        host_aliases=i.host_aliases,
                        last_session_at=sessions.get((i.host, i.tool)),
                    )
                    for i in self._settings.installs
                ),
                required_kinds=self._settings.required_kinds,
                events=events,
                since=since,
                read_at=now,
                quiet_minutes=self._settings.quiet_minutes,
            )
        )
        alarms = tuple(
            ModelLabAlarm(
                condition=CONDITION,
                subject=f"{a.host}/{a.tool}",
                detail=(
                    f"{a.tool} on {a.host}: session at {a.session_at:%Y-%m-%dT%H:%M:%SZ}, "
                    + (
                        "no hook event since"
                        if a.newest_event_at is None
                        else f"newest hook event {a.newest_event_at:%Y-%m-%dT%H:%M:%SZ}"
                    )
                ),
            )
            for a in verdict.alerts
        )
        delivered = self._alarm.handle(
            ModelLabAlarmDeliveryRequest(
                correlation_id=correlation,
                condition=CONDITION,
                outcome=EnumDeliveryCondition.ALARM
                if alarms
                else EnumDeliveryCondition.OK,
                alarms=alarms,
                evidence=verdict.alert_text,
            )
        )
        return ModelHookInstallSilenceResult(
            outcome=EnumSilenceOutcome.ALERTED if alarms else EnumSilenceOutcome.QUIET,
            alerts=verdict.alerts,
            alert_text=verdict.alert_text,
            alarm_outcome=str(delivered.outcome),
        )

    def _resolve(self) -> None:
        if self._settings_in is None:
            self._settings_in = load_settings()
        if self._events_in is None:
            self._events_in = PostgresHookInstallEventsReader(dsn=resolve_dsn(DSN_ENV))
        if self._ledger_in is None:
            self._ledger_in = PostgresLedgerRowReader(dsn=resolve_dsn(DSN_ENV))
        if self._alarm_in is None:
            self._alarm_in = HandlerLabAlarmDeliverRoute()
        self._settings = self._settings_in
        self._events = self._events_in
        self._ledger = self._ledger_in
        self._alarm = self._alarm_in

    def _last_sessions(
        self, rows: tuple[ModelLedgerRow, ...]
    ) -> dict[tuple[str, str], datetime]:
        out: dict[tuple[str, str], datetime] = {}
        for row in rows:
            if row.row_type != "CLAIM":
                continue
            cells = parse_cells(row.raw_row)
            host = cells.get("host", "").casefold()
            actor = cells.get("actor", "").casefold()
            for install in self._settings.installs:
                if host in _names(install) and actor.startswith(install.tool):
                    key = (install.host, install.tool)
                    ts = row.row_ts
                    if key not in out or ts > out[key]:
                        out[key] = ts
        return out

    def _unreadable(
        self, correlation: str, source: str, exc: Exception
    ) -> ModelHookInstallSilenceResult:
        reason = f"{source} unreadable: {type(exc).__name__}: {exc}"
        logger.error("Hook install silence source %s", reason)
        delivered = self._alarm.handle(
            ModelLabAlarmDeliveryRequest(
                correlation_id=correlation,
                condition=CONDITION,
                outcome=EnumDeliveryCondition.ALARM,
                alarms=(
                    ModelLabAlarm(condition=CONDITION, subject=source, detail=reason),
                ),
                evidence=reason,
            )
        )
        return ModelHookInstallSilenceResult(
            outcome=EnumSilenceOutcome.UNREADABLE,
            reason=reason,
            alert_text=reason,
            alarm_outcome=str(delivered.outcome),
        )


__all__ = ["CONDITION", "HandlerHookInstallSilenceAlert"]
