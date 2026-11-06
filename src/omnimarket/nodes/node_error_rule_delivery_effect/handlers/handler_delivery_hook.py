# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""In-process matching, durable no-match outbox and independent delivery probes."""

from __future__ import annotations

import hashlib
import time
from collections.abc import Mapping
from datetime import timedelta
from uuid import uuid4

from omnimarket.models.error_rule_delivery import (
    ModelDeliveryAlarmRequest,
    ModelDeliveryFailure,
    ModelDeliveryJournalEntry,
    ModelDeliveryMatch,
    ModelDeliveryOverlay,
    ModelDeliveryRequest,
    ModelDeliveryResult,
    ModelDeliverySignal,
    Surface,
)
from omnimarket.models.exception_queue import (
    ModelExceptionEmitRequest,
    ModelExceptionRow,
)
from omnimarket.protocols.protocol_error_rule_delivery import (
    ProtocolDeliveryAlarm,
    ProtocolDeliveryExceptionQueue,
    ProtocolDeliveryMatcher,
    ProtocolDeliveryPath,
)

from .delivery_journal import DeliveryJournal

SURFACES: tuple[Surface, ...] = ("tool_failure", "guard_refusal")


def event_key(failure: ModelDeliveryFailure) -> str:
    # Deliberately exclude the hook event: the nonzero-Bash fallback and
    # PostToolUseFailure are the same occurrence, including across processes.
    return hashlib.sha256(
        f"{failure.session_id}\0{failure.tool_use_id}\0{failure.surface}".encode()
    ).hexdigest()


class HandlerDeliveryHook:
    def __init__(
        self,
        overlay: ModelDeliveryOverlay,
        *,
        matcher: ProtocolDeliveryMatcher,
        alarm: ProtocolDeliveryAlarm,
        queue: ProtocolDeliveryExceptionQueue | None = None,
        paths: Mapping[Surface, ProtocolDeliveryPath] | None = None,
    ) -> None:
        self.overlay = overlay
        self.matcher, self.alarm, self.queue = matcher, alarm, queue
        self.paths = paths or {}
        self.journal = DeliveryJournal(overlay.state_dir)

    def _alarm(self, request: ModelDeliveryRequest, signal: ModelDeliverySignal) -> str:
        message = f"{signal.cause} {signal.surface}: {signal.detail}"
        try:
            result = self.alarm.handle(
                ModelDeliveryAlarmRequest(now=request.now, signals=(signal,))
            )
            if not result.raised or result.warnings:
                return f"{message}; alarm delivery failed: {result.warnings}"
        except Exception as exc:
            return f"{message}; alarm delivery failed: {type(exc).__name__}: {exc}"
        return message

    def handle(self, request: ModelDeliveryRequest) -> ModelDeliveryResult:
        try:
            if request.operation == "tick":
                return self._tick(request)
            with self.journal.locked():
                entries = self.journal.read()
                if request.operation == "failure":
                    return self._failure(request, entries)
                if request.operation == "pre_tool":
                    return self._pre_tool(request, entries)
                if request.operation == "drain":
                    return self._drain(request, entries)
                if request.traffic_surface is None:
                    raise ValueError("traffic requires traffic_surface")
                self.journal.append(
                    ModelDeliveryJournalEntry(
                        kind="traffic",
                        key=str(uuid4()),
                        at=request.now,
                        surface=request.traffic_surface,
                    )
                )
                return ModelDeliveryResult(status="TRAFFIC")
        except (OSError, ValueError) as exc:
            signal = ModelDeliverySignal(
                surface=request.failure.surface if request.failure else "tool_failure",
                cause="outbox-unread",
                detail=str(exc),
            )
            return ModelDeliveryResult(
                status="UNREAD",
                line="UNREAD delivery state",
                warnings=(self._alarm(request, signal),),
            )

    def _failure(
        self, request: ModelDeliveryRequest, entries: list[ModelDeliveryJournalEntry]
    ) -> ModelDeliveryResult:
        failure = request.failure
        if failure is None:
            raise ValueError("failure operation requires a failure")
        key = event_key(failure)
        if any(e.key == key and e.kind in ("outbox", "handled") for e in entries):
            return ModelDeliveryResult(status="DUPLICATE")
        if failure.probe_id is None:
            self.journal.append(
                ModelDeliveryJournalEntry(
                    kind="traffic", key=key, at=request.now, surface=failure.surface
                )
            )
        started = time.perf_counter()
        try:
            match = self.matcher.handle(failure)
            if match.status == "MATCH" and (
                not match.line.startswith("RULE:") or not match.class_key
            ):
                match = ModelDeliveryMatch(
                    status="UNREAD", detail="matcher returned an incomplete RULE line"
                )
            if match.status == "NO-MATCH" and not match.class_key:
                match = ModelDeliveryMatch(
                    status="UNREAD", detail="matcher returned no class key"
                )
        except Exception as exc:
            match = ModelDeliveryMatch(
                status="UNREAD", detail=f"{type(exc).__name__}: {exc}"
            )
        elapsed_ms = (time.perf_counter() - started) * 1000
        self.journal.append(
            ModelDeliveryJournalEntry(
                kind="matcher",
                key=failure.probe_id or key,
                at=request.now,
                surface=failure.surface,
                failure=failure,
                match=match,
            )
        )
        warnings = []
        if elapsed_ms > self.overlay.latency_budget_ms:
            warnings.append(
                self._alarm(
                    request,
                    ModelDeliverySignal(
                        surface=failure.surface,
                        cause="over-budget",
                        detail=f"matcher {elapsed_ms:.3f}ms > {self.overlay.latency_budget_ms}ms",
                    ),
                )
            )
        if match.status == "UNREAD":
            warnings.append(
                self._alarm(
                    request,
                    ModelDeliverySignal(
                        surface=failure.surface, cause="UNREAD", detail=match.detail
                    ),
                )
            )
            return ModelDeliveryResult(
                status="UNREAD",
                line=f"UNREAD matcher: {match.detail}",
                warnings=tuple(warnings),
            )
        # Synthetic payloads measure the real path, without polluting the queue,
        # agent context or surface traffic denominator.
        if failure.probe_id is not None:
            return ModelDeliveryResult(status=match.status, warnings=tuple(warnings))
        if match.status == "NO-MATCH":
            self.journal.append(
                ModelDeliveryJournalEntry(
                    kind="outbox",
                    key=key,
                    at=request.now,
                    surface=failure.surface,
                    failure=failure,
                    match=match,
                )
            )
            return ModelDeliveryResult(status="NO-MATCH", warnings=tuple(warnings))
        queued = self.overlay.mode == "hint" and self.overlay.queue_context
        self.journal.append(
            ModelDeliveryJournalEntry(
                kind="handled",
                key=key,
                at=request.now,
                surface=failure.surface,
                failure=failure,
                match=match,
                queued=queued,
            )
        )
        if self.overlay.mode == "shadow":
            return ModelDeliveryResult(status="SHADOW", warnings=tuple(warnings))
        if queued:
            return ModelDeliveryResult(status="QUEUED", warnings=tuple(warnings))
        output = (
            {}
            if failure.surface == "guard_refusal"
            else {
                "hookSpecificOutput": {
                    "hookEventName": failure.hook_event_name,
                    "additionalContext": match.line,
                }
            }
        )
        return ModelDeliveryResult(
            status="MATCH",
            line=match.line,
            hook_output=output,
            warnings=tuple(warnings),
        )

    def _pre_tool(
        self, request: ModelDeliveryRequest, entries: list[ModelDeliveryJournalEntry]
    ) -> ModelDeliveryResult:
        delivered = {e.key for e in entries if e.kind == "delivered"}
        pending = [
            e
            for e in entries
            if e.kind == "handled"
            and e.queued
            and e.key not in delivered
            and e.failure is not None
            and e.failure.session_id == request.session_id
        ]
        if self.overlay.mode == "shadow":
            return ModelDeliveryResult(status="SHADOW")
        lines = []
        for entry in pending:
            if entry.match is not None:
                lines.append(entry.match.line)
            self.journal.append(
                entry.model_copy(update={"kind": "delivered", "at": request.now})
            )
        line = "\n".join(lines)
        output = (
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": line,
                }
            }
            if line
            else {}
        )
        return ModelDeliveryResult(status="DELIVERED", line=line, hook_output=output)

    def _drain(
        self, request: ModelDeliveryRequest, entries: list[ModelDeliveryJournalEntry]
    ) -> ModelDeliveryResult:
        if self.queue is None:
            raise ValueError(
                "exception queue is not bound; outbox remains unacknowledged"
            )
        acknowledged = {e.key for e in entries if e.kind == "ack"}
        count = 0
        for entry in entries:
            if entry.kind != "outbox" or entry.key in acknowledged:
                continue
            if entry.match is None or entry.failure is None:
                raise ValueError("outbox entry has no failure or match")
            result = self.queue.handle(
                ModelExceptionEmitRequest(
                    state_dir=self.overlay.exception_dir,
                    now=request.now.isoformat(),
                    idempotency_key=entry.key,
                    row=ModelExceptionRow(
                        cls=self.overlay.exception_class,
                        key=entry.match.class_key,
                        source=entry.surface,
                        evidence=entry.failure.error_text,
                        tried="in-process error rule matcher: NO-MATCH",
                        proposed_fix="triage the normalized error class and bind a rule",
                    ),
                )
            )
            if result.status not in ("appended", "repeat"):
                raise ValueError(f"exception queue refused: {result.detail}")
            self.journal.append(
                entry.model_copy(update={"kind": "ack", "at": request.now})
            )
            acknowledged.add(entry.key)
            count += 1
        return ModelDeliveryResult(status="DRAINED", acknowledged=count)

    def _tick(self, request: ModelDeliveryRequest) -> ModelDeliveryResult:
        # Do not hold the journal lock while traversing an installed path: the
        # hook it launches writes matcher receipts to this same journal.
        with self.journal.locked():
            entries = self.journal.read()
        warnings = []
        seen_before = {e.key for e in entries if e.kind == "matcher"}
        missed = set()
        for surface in SURFACES:
            probes = [p for p in self.overlay.probes if p.surface == surface]
            previous = [
                e for e in entries if e.kind == "probe" and e.surface == surface
            ]
            if previous:
                last = previous[-1]
                age = (request.now - last.at).total_seconds()
                if last.key not in seen_before:
                    if age < self.overlay.probe_timeout_seconds:
                        continue
                    missed.add(last.key)
                    warnings.append(
                        self._alarm(
                            request,
                            ModelDeliverySignal(
                                surface=surface,
                                cause="probe-not-seen",
                                detail=f"synthetic failure {last.key} never reached matcher",
                            ),
                        )
                    )
                if age < self.overlay.probe_interval_seconds:
                    continue
            probe_id = str(uuid4())
            if len(probes) != 1:
                warnings.append(
                    self._alarm(
                        request,
                        ModelDeliverySignal(
                            surface=surface,
                            cause="probe-not-seen",
                            detail="overlay must declare one synthetic payload for each surface",
                        ),
                    )
                )
                continue
            probe = probes[0].model_copy(
                update={"probe_id": probe_id, "tool_use_id": probe_id}
            )
            with self.journal.locked():
                self.journal.append(
                    ModelDeliveryJournalEntry(
                        kind="probe",
                        key=probe_id,
                        at=request.now,
                        surface=surface,
                        failure=probe,
                    )
                )
            try:
                self.paths[surface].deliver(probe)
            except Exception as exc:
                warnings.append(
                    self._alarm(
                        request,
                        ModelDeliverySignal(
                            surface=surface,
                            cause="probe-not-seen",
                            detail=f"delivery path unavailable: {type(exc).__name__}: {exc}",
                        ),
                    )
                )
        with self.journal.locked():
            entries = self.journal.read()
        seen = {e.key for e in entries if e.kind == "matcher"}
        unread = [
            e
            for e in entries
            if e.kind == "matcher"
            and e.match is not None
            and e.match.status == "UNREAD"
            and request.now - timedelta(seconds=self.overlay.window_seconds)
            < e.at
            <= request.now
        ]
        for entry in unread:
            warnings.append(
                f"UNREAD {entry.surface}: matcher failed; quiet alarm raised by delivery path"
            )
        latest = {e.surface: e for e in entries if e.kind == "probe"}
        for entry in latest.values():
            if (
                entry.key not in missed
                and entry.key not in seen
                and (request.now - entry.at).total_seconds()
                >= self.overlay.probe_timeout_seconds
            ):
                warnings.append(
                    self._alarm(
                        request,
                        ModelDeliverySignal(
                            surface=entry.surface,
                            cause="probe-not-seen",
                            detail=f"synthetic failure {entry.key} never reached matcher",
                        ),
                    )
                )
        window = request.now - timedelta(seconds=self.overlay.window_seconds)
        recent = [e for e in entries if window < e.at <= request.now]
        calls: dict[Surface, int] = {}
        traffic: dict[Surface, int] = {}
        for surface in SURFACES:
            calls[surface] = sum(
                e.kind == "matcher"
                and e.surface == surface
                and e.failure is not None
                and e.failure.probe_id is None
                for e in recent
            )
            traffic[surface] = sum(
                e.kind == "traffic" and e.surface == surface for e in recent
            )
            if traffic[surface] and not calls[surface]:
                warnings.append(
                    self._alarm(
                        request,
                        ModelDeliverySignal(
                            surface=surface,
                            cause="zero-matcher-calls",
                            detail=f"{traffic[surface]} surface events and zero matcher calls in window",
                        ),
                    )
                )
        pending = any(e.key not in seen for e in latest.values())
        acknowledged = 0
        if any(e.kind == "outbox" for e in entries):
            with self.journal.locked():
                drained = self._drain(request, self.journal.read())
                acknowledged = drained.acknowledged
        return ModelDeliveryResult(
            status="ALARM" if warnings else "PROBING" if pending else "HEALTHY",
            acknowledged=acknowledged,
            warnings=tuple(warnings),
            matcher_calls=calls,
            traffic=traffic,
        )


class HandlerDeliveryHookRuntime:
    """No-argument runtime binding; deployment dependencies stay off the wire."""

    def handle(self, request: ModelDeliveryRequest) -> ModelDeliveryResult:
        from ..container.container_delivery_hook import create_handler_delivery_hook

        try:
            return create_handler_delivery_hook().handle(request)
        except (OSError, ValueError) as exc:
            from omnimarket.nodes.node_silent_failure_alarm_effect import (
                HandlerDeliveryHookAlarm,
            )

            signal = ModelDeliverySignal(
                surface=request.failure.surface if request.failure else "tool_failure",
                cause="UNREAD",
                detail=str(exc),
            )
            warnings = [f"UNREAD delivery overlay: {exc}"]
            try:
                alarm = HandlerDeliveryHookAlarm().handle(
                    ModelDeliveryAlarmRequest(now=request.now, signals=(signal,))
                )
                warnings.extend(alarm.warnings)
            except Exception as alarm_error:
                warnings.append(f"UNREAD quiet alarm: {alarm_error}")
            return ModelDeliveryResult(
                status="UNREAD", line=warnings[0], warnings=tuple(warnings)
            )
