# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Host observer: reports the runs of processes that cannot emit (OMN-20801).

Canonical definition-B shape: ``handle(request) -> result``. For each overlay
entry on this host that an observer reports for, the handler reads the evidence
the entry names, journals one run event per run (``run_id`` makes a re-read add
nothing), notes a run left open past ``max_runtime_seconds`` as OVERRUN, and
reports every unit of ours that has no entry as UNDECLARED. Every event is
appended to the on-disk journal before it is sent, and sent oldest first, so a
broker outage delays events and loses none.

Evidence sources plug in through ``ProtocolRunEvidenceSource``; this module
registers the receipts-file, log-line and launchd-state sources. An entry whose
evidence source has no reader here is reported UNOBSERVABLE, not skipped.
"""

import logging
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from omnibase_core.enums.enum_liveness_state import EnumLivenessState
from pydantic import BaseModel

from omnimarket.models.liveness.model_automation_liveness import (
    VERDICT_STATE,
    EnumAutomationEmitter,
    EnumAutomationLivenessEvent,
    EnumAutomationLivenessReason,
    EnumAutomationLivenessVerdict,
    EnumAutomationProcessState,
    EnumAutomationRunPhase,
    ModelAutomationHeartbeat,
    ModelAutomationLivenessEntry,
    ModelAutomationLivenessVerdictEvent,
    ModelAutomationRunObserved,
    automation_liveness_topics,
    load_automation_liveness_overlay,
)
from omnimarket.nodes.node_automation_run_observer_effect.handlers.event_journal import (
    EventJournal,
    ModelJournalEntry,
    ProtocolObserverEventSink,
    event_id_for,
)
from omnimarket.nodes.node_automation_run_observer_effect.handlers.kafka_event_sink import (
    KafkaObserverEventSink,
)
from omnimarket.nodes.node_automation_run_observer_effect.handlers.undeclared_census import (
    run_census,
)
from omnimarket.nodes.node_automation_run_observer_effect.models import (
    ModelAutomationRunObserverRequest,
    ModelAutomationRunObserverResult,
    ModelObserverFinding,
    ModelObserverState,
    ModelProcessCursor,
    load_state,
    save_state,
)
from omnimarket.nodes.node_automation_run_observer_effect.models.model_observer_state import (
    SEEN_RUN_KEYS_KEPT,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.host_command_runner import (
    ProtocolHostCommandRunner,
    SubprocessHostCommand,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.protocol_run_evidence_source import (
    ModelEvidenceReadContext,
    ModelObservedRun,
    ProtocolRunEvidenceSource,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.source_launchd_state import (
    SourceLaunchdState,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.source_log_line import (
    SourceLogLine,
)
from omnimarket.nodes.node_automation_run_observer_effect.sources.source_receipts_file import (
    SourceReceiptsFile,
)

logger = logging.getLogger(__name__)

_STATE_FILE = "observer-state.json"
_JOURNAL_FILE = "event-journal.jsonl"
_V = EnumAutomationLivenessVerdict
_R = EnumAutomationLivenessReason


class _Pending(BaseModel):
    topic: str
    key: str
    payload: dict[str, object]


class HandlerAutomationRunObserver:
    def __init__(
        self,
        *,
        sources: Sequence[ProtocolRunEvidenceSource] | None = None,
        runner: ProtocolHostCommandRunner | None = None,
        sink: ProtocolObserverEventSink | None = None,
    ) -> None:
        self._runner: ProtocolHostCommandRunner = runner or SubprocessHostCommand()
        chosen = sources or (
            SourceReceiptsFile(),
            SourceLogLine(),
            SourceLaunchdState(self._runner),
        )
        self._sources = {source.source: source for source in chosen}
        self._sink = sink
        self._topics = automation_liveness_topics()

    def handle(
        self, request: ModelAutomationRunObserverRequest
    ) -> ModelAutomationRunObserverResult:
        overlay = load_automation_liveness_overlay(Path(request.overlay_path))
        state_dir = Path(request.state_dir)
        state_dir.mkdir(parents=True, exist_ok=True)
        state = load_state(state_dir / _STATE_FILE)
        if state.first_tick_at is None:
            state.first_tick_at = request.now
        state.polls += 1

        own = [e for e in overlay.processes if e.host == request.host]
        pending: list[_Pending] = []
        findings: list[ModelObserverFinding] = []
        read = [
            e
            for e in own
            if e.emitter is EnumAutomationEmitter.OBSERVER
            and e.state is EnumAutomationProcessState.ACTIVE
        ]
        runs_emitted = unseen = 0
        for entry in read:
            emitted, missed = self._observe_entry(
                entry, request, state, pending, findings
            )
            runs_emitted += emitted
            unseen += missed
        self._census(request, own, state, pending, findings)
        pending.append(self._heartbeat(request, state))

        journal = EventJournal(state_dir / _JOURNAL_FILE)
        journal.append(self._journal_entries(pending, state))
        save_state(state_dir / _STATE_FILE, state)
        sent, left, error = journal.flush(self._sink or KafkaObserverEventSink())
        if error is not None:
            logger.error("observer journal flush stopped: %s", error)
        return ModelAutomationRunObserverResult(
            host=request.host,
            observed_at=request.now,
            entries_read=len(read),
            runs_emitted=runs_emitted,
            unseen_runs=unseen,
            findings=tuple(findings),
            events_journaled=len(pending),
            events_published=sent,
            journal_backlog=left,
            journal_corrupt_lines=journal.corrupt_lines,
            flush_error=error,
        )

    # ------------------------------------------------------------------ entries

    def _observe_entry(
        self,
        entry: ModelAutomationLivenessEntry,
        request: ModelAutomationRunObserverRequest,
        state: ModelObserverState,
        pending: list[_Pending],
        findings: list[ModelObserverFinding],
    ) -> tuple[int, int]:
        cursor = state.cursors.setdefault(entry.process_id, ModelProcessCursor())
        digest = entry.digest()
        source = self._sources.get(entry.evidence.source) if entry.evidence else None
        unreadable: str | None
        if source is None:
            unreadable = (
                "no reader for evidence source "
                f"{entry.evidence.source.value if entry.evidence else 'none'} "
                "on this observer"
            )
            runs: tuple[ModelObservedRun, ...] = ()
        else:
            try:
                result = source.read(
                    ModelEvidenceReadContext(
                        entry=entry,
                        cursor=cursor,
                        now=request.now,
                        launchd_domain=request.launchd_domain,
                    )
                )
            except Exception as exc:
                unreadable, runs = f"{type(exc).__name__}: {exc}", ()
            else:
                unreadable, runs = result.unreadable, result.runs
                if unreadable is None:
                    cursor = result.cursor
                    state.cursors[entry.process_id] = cursor
        if unreadable is not None:
            if cursor.unreadable_reported != unreadable:
                cursor.unreadable_reported = unreadable
                self._report(
                    entry,
                    _V.UNOBSERVABLE,
                    _R.EVIDENCE_UNREADABLE,
                    request.now,
                    request.now,
                    unreadable,
                    digest,
                    pending,
                    findings,
                )
            return 0, 0
        cursor.unreadable_reported = None
        emitted, unseen = self._emit_runs(entry, runs, cursor, request, digest, pending)
        self._report_overruns(entry, cursor, request, digest, pending, findings)
        return emitted, unseen

    def _emit_runs(
        self,
        entry: ModelAutomationLivenessEntry,
        runs: tuple[ModelObservedRun, ...],
        cursor: ModelProcessCursor,
        request: ModelAutomationRunObserverRequest,
        digest: str,
        pending: list[_Pending],
    ) -> tuple[int, int]:
        finished_ids = {run.run_id for run in runs if run.finished_at is not None}
        emitted = unseen = 0
        for run in runs:
            phase = (
                EnumAutomationRunPhase.FINISHED
                if run.finished_at is not None
                else EnumAutomationRunPhase.STARTED
            )
            if phase is EnumAutomationRunPhase.FINISHED:
                cursor.open_runs.pop(run.run_id, None)
                if run.run_id in cursor.overrun_reported:
                    cursor.overrun_reported.remove(run.run_id)
            elif f"{run.run_id}|finished" not in cursor.emitted:
                cursor.open_runs[run.run_id] = run.started_at
            if phase is EnumAutomationRunPhase.STARTED and run.run_id in finished_ids:
                continue
            key = f"{run.run_id}|{phase.value}"
            if key in cursor.emitted:
                continue
            cursor.emitted.append(key)
            del cursor.emitted[:-SEEN_RUN_KEYS_KEPT]
            event = ModelAutomationRunObserved(
                process_id=entry.process_id,
                host=entry.host,
                run_id=run.run_id,
                phase=phase,
                started_at=run.started_at,
                finished_at=run.finished_at,
                outcome=run.outcome,
                exit_code=run.exit_code,
                did_work_count=run.did_work_count,
                demand_count=run.demand_count,
                unseen_runs=run.unseen_runs,
                evidence_ref=run.evidence_ref,
                emitter=EnumAutomationEmitter.OBSERVER,
                observed_at=request.now,
                contract_digest=digest,
            )
            pending.append(
                self._pending(
                    EnumAutomationLivenessEvent.RUN_OBSERVED, entry.process_id, event
                )
            )
            emitted += 1
            unseen += run.unseen_runs
        return emitted, unseen

    def _report_overruns(
        self,
        entry: ModelAutomationLivenessEntry,
        cursor: ModelProcessCursor,
        request: ModelAutomationRunObserverRequest,
        digest: str,
        pending: list[_Pending],
        findings: list[ModelObserverFinding],
    ) -> None:
        if entry.max_runtime_seconds is None:
            return
        bound = timedelta(seconds=entry.max_runtime_seconds)
        for run_id, started in sorted(cursor.open_runs.items()):
            if request.now - started <= bound or run_id in cursor.overrun_reported:
                continue
            cursor.overrun_reported.append(run_id)
            self._report(
                entry,
                _V.OVERRUN,
                _R.RUN_OPEN_PAST_MAX_RUNTIME,
                started + bound,
                request.now,
                f"run {run_id} started {started.isoformat()} has no completion "
                f"after {entry.max_runtime_seconds} s",
                digest,
                pending,
                findings,
            )

    # ------------------------------------------------------------------ census

    def _census(
        self,
        request: ModelAutomationRunObserverRequest,
        own: list[ModelAutomationLivenessEntry],
        state: ModelObserverState,
        pending: list[_Pending],
        findings: list[ModelObserverFinding],
    ) -> None:
        declared = frozenset(
            name
            for e in own
            for name in (
                e.trigger.native_id,
                e.evidence.locator if e.evidence else e.trigger.native_id,
            )
        )
        census = run_census(request.host, request.census, declared, self._runner)
        if census.unreadable is not None:
            logger.error("undeclared census could not read: %s", census.unreadable)
            findings.append(
                ModelObserverFinding(
                    process_id=f"{request.host}/undeclared-census",
                    verdict=_V.UNOBSERVABLE,
                    reason=_R.EVIDENCE_UNREADABLE,
                    detail=census.unreadable,
                )
            )
            return
        present = {unit.process_id for unit in census.units}
        for gone in set(state.undeclared_first_seen) - present:
            del state.undeclared_first_seen[gone]
        for unit in census.units:
            if unit.process_id in state.undeclared_first_seen:
                continue
            state.undeclared_first_seen[unit.process_id] = request.now
            event = ModelAutomationLivenessVerdictEvent(
                process_id=unit.process_id,
                host=request.host,
                verdict=_V.UNDECLARED,
                state=VERDICT_STATE[_V.UNDECLARED],
                reason=_R.PROCESS_WITHOUT_ENTRY,
                verdict_since=request.now,
                evaluated_at=request.now,
                detail=unit.detail,
            )
            pending.append(
                self._pending(
                    EnumAutomationLivenessEvent.LIVENESS_VERDICT, unit.process_id, event
                )
            )
            findings.append(
                ModelObserverFinding(
                    process_id=unit.process_id,
                    verdict=_V.UNDECLARED,
                    reason=_R.PROCESS_WITHOUT_ENTRY,
                    detail=unit.detail,
                )
            )

    # ------------------------------------------------------------------ events

    def _report(
        self,
        entry: ModelAutomationLivenessEntry,
        verdict: EnumAutomationLivenessVerdict,
        reason: EnumAutomationLivenessReason,
        since: datetime,
        now: datetime,
        detail: str,
        digest: str,
        pending: list[_Pending],
        findings: list[ModelObserverFinding],
    ) -> None:
        state: EnumLivenessState = VERDICT_STATE[verdict]
        event = ModelAutomationLivenessVerdictEvent(
            process_id=entry.process_id,
            host=entry.host,
            verdict=verdict,
            state=state,
            reason=reason,
            verdict_since=since,
            evaluated_at=now,
            contract_digest=digest,
            detail=detail,
        )
        pending.append(
            self._pending(
                EnumAutomationLivenessEvent.LIVENESS_VERDICT, entry.process_id, event
            )
        )
        findings.append(
            ModelObserverFinding(
                process_id=entry.process_id,
                verdict=verdict,
                reason=reason,
                detail=detail,
            )
        )

    def _heartbeat(
        self, request: ModelAutomationRunObserverRequest, state: ModelObserverState
    ) -> _Pending:
        process_id = f"{request.host}/automation-run-observer"
        assert state.first_tick_at is not None
        beat = ModelAutomationHeartbeat(
            process_id=process_id,
            host=request.host,
            process_started_at=state.first_tick_at,
            progress_counter=state.polls,
            last_progress_at=request.now,
            emitted_at=request.now,
        )
        return self._pending(EnumAutomationLivenessEvent.HEARTBEAT, process_id, beat)

    def _pending(
        self, event: EnumAutomationLivenessEvent, key: str, model: BaseModel
    ) -> _Pending:
        return _Pending(
            topic=self._topics[event], key=key, payload=model.model_dump(mode="json")
        )

    @staticmethod
    def _journal_entries(
        pending: list[_Pending], state: ModelObserverState
    ) -> list[ModelJournalEntry]:
        entries = []
        for item in pending:
            entries.append(
                ModelJournalEntry(
                    seq=state.next_seq,
                    topic=item.topic,
                    key=item.key,
                    event_id=event_id_for(item.topic, item.payload),
                    payload=item.payload,
                )
            )
            state.next_seq += 1
        return entries
