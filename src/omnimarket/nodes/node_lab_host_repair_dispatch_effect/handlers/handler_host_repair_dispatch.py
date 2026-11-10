# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consume the disk watermark alarm and start one repair lane per host and alarm (OMN-23389).

On 2026-11-09 a full disk on one lab host was repaired by lanes that an orchestrator wrote by hand, pinned to the
full host or assuming a filesystem it had not read. Here the alarm itself starts the repair:

* the dedupe key is ``<host>|<alarm>``, kept in a durable store, so a second alarm for the same host and alarm
  while the episode stands dispatches no second lane;
* the alarm repeating after the backoff means the repair did not clear it, so the node dispatches the next attempt,
  up to the overlay's retry budget, then writes one escalation row and dispatches no more;
* a reading below the page mark ends the episode;
* the brief carries the df and findmnt reading measured on the target host, never an assumed layout. A host that
  cannot be measured is not given a brief that guesses: the attempt is spent and recorded;
* the attempt is recorded before anything is started, so a restart between recording and starting loses nothing
  and the lane name (episode and attempt) is the same on a replay.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_lab_host_repair_dispatch_effect.handlers.adapters import (
    ArgvDispatcher,
    ArgvEscalation,
    DispatchError,
    InMemoryDispatcher,
    InMemoryEscalation,
    InMemoryMeasurement,
    InMemoryStateStore,
    JsonFileStateStore,
    MeasurementError,
    SshMeasurement,
)
from omnimarket.nodes.node_lab_host_repair_dispatch_effect.models.model_host_repair import (
    EnumHostRepairAction,
    ModelHostMeasurement,
    ModelHostRepairOverlay,
    ModelHostRepairRequest,
    ModelHostRepairResult,
    ModelRepairLaneSpec,
    ModelRepairState,
)
from omnimarket.nodes.node_lab_host_repair_dispatch_effect.protocols import (
    ProtocolEscalationSink,
    ProtocolHostMeasurement,
    ProtocolRepairDispatcher,
    ProtocolRepairStateStore,
)

NODE_NAME = "node_lab_host_repair_dispatch_effect"
OVERLAY_ENV = "ONEX_LAB_HOST_REPAIR_OVERLAY"
STAMP = "%Y-%m-%dT%H:%M:%SZ"


class RepairRefusalError(ValueError):
    """The overlay is unreadable or an alarm time is not a time."""


def load_overlay(overlay_file: str = "") -> ModelHostRepairOverlay:
    if overlay_file:
        try:
            document = yaml.safe_load(Path(overlay_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise RepairRefusalError(
                f"overlay {overlay_file} is unreadable ({type(exc).__name__})"
            ) from None
        return load_structured_overlay(
            NODE_NAME, ModelHostRepairOverlay, explicit=document
        )
    return load_structured_overlay(
        NODE_NAME, ModelHostRepairOverlay, file_env=OVERLAY_ENV
    )


def parse_stamp(value: str) -> datetime:
    try:
        return datetime.strptime(value, STAMP).replace(tzinfo=UTC)
    except ValueError:
        raise RepairRefusalError(f"{value!r} is not a {STAMP} time") from None


def expand_home(value: str, home: Path) -> Path:
    return home / value[len("$HOME/") :] if value.startswith("$HOME/") else Path(value)


def episode_token(read_at: str) -> str:
    return re.sub(r"[^0-9]", "", read_at)[:12]


class HandlerHostRepairDispatch:
    """One alarm in, one decision out. Ports are injected: the defaults keep everything in memory."""

    def __init__(
        self,
        *,
        store: ProtocolRepairStateStore | None = None,
        measurement: ProtocolHostMeasurement | None = None,
        dispatcher: ProtocolRepairDispatcher | None = None,
        escalation: ProtocolEscalationSink | None = None,
        overlay: ModelHostRepairOverlay | None = None,
        home: Path | None = None,
        real: bool = False,
    ) -> None:
        self._store = store
        self._measurement = measurement
        self._dispatcher = dispatcher
        self._escalation = escalation
        self._overlay = overlay
        self._home = home if home is not None else Path.home()
        self._real = real

    # ------------------------------------------------------------------ ports

    def _ports(
        self, overlay: ModelHostRepairOverlay
    ) -> tuple[
        ProtocolRepairStateStore,
        ProtocolHostMeasurement,
        ProtocolRepairDispatcher,
        ProtocolEscalationSink,
    ]:
        if self._real:
            store = self._store or JsonFileStateStore(
                expand_home(overlay.state_file, self._home)
            )
            return (
                store,
                self._measurement
                or SshMeasurement(
                    overlay.measure_argv,
                    overlay.measure_separator,
                    overlay.measure_timeout_seconds,
                ),
                self._dispatcher
                or ArgvDispatcher(
                    overlay.dispatch_argv, overlay.dispatch_timeout_seconds
                ),
                self._escalation
                or ArgvEscalation(
                    overlay.escalate_argv, overlay.dispatch_timeout_seconds
                ),
            )
        self._store = self._store or InMemoryStateStore()
        self._measurement = self._measurement or InMemoryMeasurement()
        self._dispatcher = self._dispatcher or InMemoryDispatcher()
        self._escalation = self._escalation or InMemoryEscalation()
        return self._store, self._measurement, self._dispatcher, self._escalation

    # ------------------------------------------------------------------ decision

    def handle(self, request: ModelHostRepairRequest) -> ModelHostRepairResult:
        overlay = self._overlay or load_overlay(request.overlay_file)
        store, measurement, dispatcher, escalation = self._ports(overlay)
        alarm = request.alarm
        key = f"{alarm.host_name}|{overlay.alarm_name}"
        now = parse_stamp(request.now) if request.now else datetime.now(UTC)
        state = store.get(key)

        def result(
            action: EnumHostRepairAction,
            *,
            attempt: int = 0,
            lane: str = "",
            reason: str = "",
            latency_seconds: int | None = None,
            latency_exceeded: bool = False,
            brief_file: str = "",
        ) -> ModelHostRepairResult:
            return ModelHostRepairResult(
                action=action,
                key=key,
                host_name=alarm.host_name,
                attempt=attempt,
                lane=lane,
                latency_seconds=latency_seconds,
                latency_exceeded=latency_exceeded,
                brief_file=brief_file,
                reason=reason,
            )

        if alarm.level != "page":
            if state is None:
                return result(
                    EnumHostRepairAction.IGNORED,
                    reason=f"level {alarm.level} and no episode stands",
                )
            store.delete(key)
            return result(
                EnumHostRepairAction.CLEARED,
                attempt=state.attempts,
                reason=f"level {alarm.level} ends the episode after {state.attempts} attempt(s)",
            )

        if state is not None and state.escalated:
            return result(
                EnumHostRepairAction.ESCALATED_ALREADY,
                attempt=state.attempts,
                reason="the retry budget is spent and the escalation row is written",
            )
        if state is not None:
            wait = overlay.backoff_seconds[
                min(state.attempts, len(overlay.backoff_seconds)) - 1
            ]
            due = parse_stamp(state.last_dispatch_at).timestamp() + wait
            if now.timestamp() < due:
                return result(
                    EnumHostRepairAction.DEDUPED,
                    attempt=state.attempts,
                    lane=state.lane,
                    reason=f"attempt {state.attempts} stands for {int(due - now.timestamp())} s more",
                )
            if state.attempts >= overlay.retry_budget:
                escalation.escalate(
                    f"host repair for {alarm.host_name} ({overlay.alarm_name}) spent its budget of "
                    f"{overlay.retry_budget} attempt(s) and the alarm still stands; last lane {state.lane}"
                    + (f"; last error {state.last_error}" if state.last_error else "")
                )
                store.put(state.model_copy(update={"escalated": True}))
                return result(
                    EnumHostRepairAction.ESCALATED,
                    attempt=state.attempts,
                    lane=state.lane,
                    reason="retry budget spent",
                )

        attempt = (state.attempts if state else 0) + 1
        episode = state.episode if state else episode_token(alarm.read_at)
        lane = f"{overlay.lane_prefix}-{alarm.host_name}-{episode}-a{attempt}"
        recorded = ModelRepairState(
            key=key,
            episode=episode,
            attempts=attempt,
            last_dispatch_at=now.strftime(STAMP),
            lane=lane,
        )
        store.put(recorded)
        retried = state is not None
        latency = int(now.timestamp() - parse_stamp(alarm.read_at).timestamp())

        try:
            measured = measurement.measure(alarm.host_name)
        except MeasurementError as exc:
            store.put(recorded.model_copy(update={"last_error": f"measure: {exc}"}))
            return result(
                EnumHostRepairAction.MEASURE_FAILED,
                attempt=attempt,
                lane=lane,
                reason=str(exc),
            )
        spec = ModelRepairLaneSpec(
            lane=lane,
            target=alarm.host_name,
            ticket=overlay.repair_ticket,
            parent=overlay.parent_lane,
            brief=self._brief(overlay, request, attempt, measured),
            brief_file=str(expand_home(overlay.brief_dir, self._home) / f"{lane}.md"),
            ledger=request.ledger_file,
        )
        try:
            dispatcher.dispatch(spec)
        except DispatchError as exc:
            store.put(recorded.model_copy(update={"last_error": f"dispatch: {exc}"}))
            return result(
                EnumHostRepairAction.DISPATCH_FAILED,
                attempt=attempt,
                lane=lane,
                reason=str(exc),
            )
        return result(
            EnumHostRepairAction.RETRIED
            if retried
            else EnumHostRepairAction.DISPATCHED,
            attempt=attempt,
            lane=lane,
            latency_seconds=latency,
            latency_exceeded=latency > overlay.alarm_to_repair_latency_seconds,
            brief_file=spec.brief_file,
        )

    @staticmethod
    def _brief(
        overlay: ModelHostRepairOverlay,
        request: ModelHostRepairRequest,
        attempt: int,
        measured: ModelHostMeasurement,
    ) -> str:
        alarm = request.alarm
        worst = max((v.used_percent for v in alarm.volumes), default=0.0)
        text = overlay.brief_template
        for name, value in {
            "host": alarm.host_name,
            "attempt": str(attempt),
            "budget": str(overlay.retry_budget),
            "alarm": overlay.alarm_name,
            "read_at": alarm.read_at,
            "used_percent": f"{worst:.1f}",
            "df": measured.df,
            "findmnt": measured.findmnt,
        }.items():
            text = text.replace("{" + name + "}", value)
        return text
