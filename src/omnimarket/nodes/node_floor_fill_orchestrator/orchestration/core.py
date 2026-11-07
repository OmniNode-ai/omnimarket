# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""FloorFillOrchestrator: the one writer of every floor lane (plan T13).

The one rule is the state row, not a lock. There is one row per ``floor_fill.scope`` and every write
of it is conditional on the stored ``seq`` and made before any intent it authorises is issued. A write
whose version is stale ends the command ``FAILED`` with ``STALE-STATE``: nothing is started from the
write that failed, and the answer lists the starts earlier writes of the same command had committed.
A row that cannot be read or parsed makes the command send nothing, write nothing and answer ``FAILED``
with ``UNKNOWN fill-state:<cause>``; it is never read as empty.

A pass (``tick``) is S0 to S6:

S0 read the row (no row is not an unreadable row); S1 read the facts through the seams; S2 resolve every
RESERVED and SENT entry by the reducer's rules 2 to 4; S3 stop when a source could not be read (nothing is
sent); S4 decide (the brake and the stop signal choose nothing before any Linear call); S5 walk each line's
candidates, mint if needed, reserve (a write) and then start; S6 recompute the verdicts from the resulting
row, render the lines and store the answer.

The orchestrator does no I/O itself: the floor, plan texts, brake and stop signals come through the fact
source, the ledger through the ledger seam, the pool through the pool seam, the tickets and the process
start through their effect seams.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timedelta

from pydantic import ValidationError

from omnimarket.nodes.node_floor_fill_candidates_compute.handlers import (
    ModelFloorEntryTicketVerdict,
    ModelFloorFillCandidatesRequest,
    ModelFloorFillCandidatesResult,
    ModelFloorFillLine,
    ModelFloorFillStateEntry,
    ModelFloorRecordRead,
    ModelFloorTaskPayloadVerdict,
)
from omnimarket.nodes.node_floor_fill_candidates_compute.handlers.handler_floor_fill_candidates import (
    HandlerFloorFillCandidates,
)
from omnimarket.nodes.node_floor_fill_lines_compute.handlers import (
    EnumFloorFillLinesEntryState,
    EnumFloorFillLinesStartKind,
    EnumFloorFillLinesVerdict,
    ModelFloorFillLinesEntry,
    ModelFloorFillLinesLine,
    ModelFloorFillLinesRequest,
    ModelFloorFillLinesStartResult,
    ModelFloorFillLinesUntickable,
)
from omnimarket.nodes.node_floor_fill_lines_compute.handlers.handler_floor_fill_lines import (
    HandlerFloorFillLines,
)
from omnimarket.nodes.node_floor_fill_orchestrator.models import (
    EXIT_CODE_BY_STATUS,
    EnumFloorFillCommand,
    EnumFloorFillStatus,
    ModelFloorFillAnswer,
    ModelFloorFillConfig,
    ModelFloorFillKeyResult,
    ModelFloorFillStoredAnswer,
)
from omnimarket.nodes.node_floor_fill_orchestrator.orchestration.ports import (
    FloorFillUnreadableError,
    ModelLaneStart,
    ProtocolFloorClock,
    ProtocolFloorFactSource,
    ProtocolFloorLaneStarter,
    ProtocolFloorLedger,
    ProtocolFloorPool,
    ProtocolFloorStateStore,
    ProtocolFloorTickets,
)
from omnimarket.nodes.node_floor_fill_reducer.handlers import (
    EnumFloorFillDropReason,
    EnumFloorFillEntryState,
    ModelFloorFillObservation,
    ModelFloorFillReduceInput,
    ModelFloorFillReduceOutput,
    ModelFloorFillReservedChecked,
    ModelFloorFillReserveRequested,
    ModelFloorFillSentChecked,
    ModelFloorFillStartFailed,
    ModelFloorFillState,
    ModelFloorFillTicketStored,
    line_and_task,
)
from omnimarket.nodes.node_floor_fill_reducer.handlers.handler_floor_fill_reducer import (
    HandlerFloorFillReducer,
)

# The class a floor task is sent as: the name the open-count brake and the stop signal answer to.
FLOOR_TASK_CLASS = "floor-task"
ANSWER_ROW_SUFFIX = ":answer"
LINE_STALE_STATE = "STALE-STATE"
SOURCES_UNREAD_ORDER = ("floor", "pool", "brake", "stop-signal", "ledger")


class StaleStateError(Exception):
    """A conditional write found a version other than the one this command read."""


class _Pass:
    """The mutable record of one command: the row as this command last wrote or read it, and what it did."""

    def __init__(self, state: ModelFloorFillState, now: datetime) -> None:
        self.state = state
        self.now = now
        self.writes = 0
        self.results: list[ModelFloorFillKeyResult] = []
        self.unread: list[str] = []
        self.ledger_unread = False
        self.free_slots = 0
        self.started = 0
        self.ticket_verdicts: dict[str, ModelFloorEntryTicketVerdict] = {}
        self.landing_only = False
        self.stop_cause: str | None = None


def _line_of(key: str) -> str:
    return line_and_task(key)[0]


def _task_of(key: str) -> str:
    return line_and_task(key)[1]


def _fill(argv: tuple[str, ...], values: Mapping[str, str]) -> tuple[str, ...]:
    out = []
    for element in argv:
        for name, value in values.items():
            element = element.replace("{" + name + "}", value)
        out.append(element)
    return tuple(out)


class FloorFillOrchestrator:
    """Runs ``tick``, ``send_key``, ``inspect`` and ``lines`` against injected seams."""

    def __init__(
        self,
        *,
        config: ModelFloorFillConfig,
        store: ProtocolFloorStateStore,
        facts: ProtocolFloorFactSource,
        pool: ProtocolFloorPool,
        ledger: ProtocolFloorLedger,
        tickets: ProtocolFloorTickets,
        starter: ProtocolFloorLaneStarter,
        clock: ProtocolFloorClock,
        candidates: HandlerFloorFillCandidates,
        reducer: HandlerFloorFillReducer,
        lines: HandlerFloorFillLines,
    ) -> None:
        self._config = config
        self._store = store
        self._facts = facts
        self._pool = pool
        self._ledger = ledger
        self._tickets = tickets
        self._starter = starter
        self._clock = clock
        self._candidates = candidates
        self._reducer = reducer
        self._lines = lines

    # ------------------------------------------------------------------ commands

    def run(
        self, command: EnumFloorFillCommand, key: str | None = None
    ) -> ModelFloorFillAnswer:
        """Run one command; every outcome is a typed answer, never an exception."""
        try:
            if command is EnumFloorFillCommand.LINES:
                return self._lines_command()
            if command is EnumFloorFillCommand.INSPECT:
                return self._inspect()
            if command is EnumFloorFillCommand.SEND_KEY:
                if not key:
                    return self._failed(command, "UNKNOWN fill-state:no-key")
                return self._pass(command, key)
            return self._pass(command, None)
        except FloorFillUnreadableError as error:
            return self._failed(command, f"UNKNOWN fill-state:{error.cause}")

    # ------------------------------------------------------------------ the pass

    def _pass(
        self, command: EnumFloorFillCommand, only_key: str | None
    ) -> ModelFloorFillAnswer:
        # S0
        state = self._read_state()
        ctx = _Pass(state, self._clock.now())
        try:
            # S1
            floor_text = self._read_facts(ctx)
            # S2
            self._resolve(ctx)
            plan_texts = self._plan_texts(ctx, floor_text)
            floor_unread = self._floor_unread(ctx, floor_text)
            if floor_unread is not None:
                ctx.unread.append(f"floor:{floor_unread}")
            # S3
            if self._stopped_at_s3(ctx):
                return self._finish(command, ctx, hold="none", floor=None)
            assert floor_text is not None
            # S4
            hold = self._hold(ctx)
            payloads = tuple(self._tickets.payload_verdicts(floor_text, plan_texts))
            if hold == "none":
                ctx.ticket_verdicts = self._entry_ticket_verdicts(ctx)
            verdicts = self._verdicts(ctx, floor_text, payloads)
            # S5
            if hold == "none":
                self._send(ctx, verdicts, only_key)
                verdicts = self._verdicts(ctx, floor_text, payloads)
            elif only_key is not None:
                self._refuse(ctx, only_key, f"held:{hold}")
            return self._finish(command, ctx, hold=hold, floor=verdicts)
        except StaleStateError:
            return self._stale(command, ctx)

    # S0

    def _read_state(self) -> ModelFloorFillState:
        read = self._store.load(self._config.scope)
        return self._decode_state(read.payload, read.version)

    @staticmethod
    def _decode_state(payload: str | None, version: int) -> ModelFloorFillState:
        if payload is None:
            return ModelFloorFillState()
        try:
            state = ModelFloorFillState.model_validate_json(payload)
        except (ValidationError, ValueError) as error:
            raise FloorFillUnreadableError("unparseable") from error
        if state.seq != version:
            raise FloorFillUnreadableError("seq-mismatch")
        return state

    # S1

    def _read_facts(self, ctx: _Pass) -> str | None:
        floor_text: str | None = None
        try:
            floor_text = self._facts.read_floor(self._config.url)
        except Exception as error:
            ctx.unread.append(f"floor:{_cause(error)}")
        try:
            ctx.free_slots = self._pool.free_slots()
        except Exception as error:
            ctx.unread.append(f"pool:{_cause(error)}")
        try:
            ctx.landing_only = self._facts.landing_only()
        except Exception as error:
            ctx.unread.append(f"brake:{_cause(error)}")
        try:
            ctx.stop_cause = self._facts.stop_refuses(FLOOR_TASK_CLASS)
        except Exception as error:
            ctx.unread.append(f"stop-signal:{_cause(error)}")
        return floor_text

    def _plan_texts(self, ctx: _Pass, floor_text: str | None) -> dict[str, str | None]:
        if floor_text is None:
            return {}
        try:
            record = ModelFloorRecordRead.model_validate_json(floor_text)
        except (ValidationError, ValueError):
            return {}
        texts: dict[str, str | None] = {}
        for line in record.lines:
            try:
                texts[line.line_id] = self._facts.read_plan(
                    self._config.plans_root, line.line_id
                )
            except Exception:
                texts[line.line_id] = None
        return texts

    def _floor_unread(self, ctx: _Pass, floor_text: str | None) -> str | None:
        """The cause a floor that was read cannot be trusted (absent, malformed, old), else None."""
        if floor_text is None:
            return None  # already recorded as unread by S1
        result = self._candidates.handle(
            self._candidates_request(ctx, floor_text, (), ())
        )
        return result.unread_cause if result.verdict == "FLOOR-UNREAD" else None

    # S2

    def _resolve(self, ctx: _Pass) -> None:
        """Resolve every RESERVED and SENT entry; an unreadable ledger resolves nothing."""
        pending = [
            e
            for e in ctx.state.entries.values()
            if e.state is not EnumFloorFillEntryState.NONE
        ]
        if not pending:
            return
        answers: dict[str, bool] = {}
        try:
            for entry in pending:
                assert entry.lane is not None
                if entry.state is EnumFloorFillEntryState.RESERVED:
                    answers[entry.key] = self._ledger.claim_found(entry.lane, entry.key)
                else:
                    answers[entry.key] = self._ledger.lane_ended(entry.lane)
        except Exception as error:
            ctx.ledger_unread = True
            ctx.unread.append(f"ledger:{_cause(error)}")
            return
        for entry in pending:
            assert entry.lane is not None
            obs: ModelFloorFillObservation
            if entry.state is EnumFloorFillEntryState.RESERVED:
                obs = ModelFloorFillReservedChecked(
                    key=entry.key,
                    attempt=entry.attempt,
                    lane=entry.lane,
                    claim_found=answers[entry.key],
                    reserved_timeout_seconds=self._config.reserved_timeout_seconds,
                    now=ctx.now,
                )
            else:
                obs = ModelFloorFillSentChecked(
                    key=entry.key,
                    attempt=entry.attempt,
                    lane=entry.lane,
                    lane_ended=answers[entry.key],
                    sent_window_minutes=self._config.sent_window_minutes,
                    now=ctx.now,
                )
            out = self._reduce(ctx.state, obs)
            # Write the row only when a resolution changed it.
            if out.dropped_reason is None and out.state.entries != ctx.state.entries:
                self._write(ctx, out.state)

    # S3

    def _stopped_at_s3(self, ctx: _Pass) -> bool:
        return bool(ctx.unread)

    # S4

    def _hold(self, ctx: _Pass) -> str:
        if ctx.landing_only:
            return "open-count"
        if ctx.stop_cause is not None:
            return f"stop-signal:{ctx.stop_cause}"
        return "none"

    def _entry_ticket_verdicts(
        self, ctx: _Pass
    ) -> dict[str, ModelFloorEntryTicketVerdict]:
        held = {
            e.key: e.ticket
            for e in ctx.state.entries.values()
            if e.ticket is not None and e.state is EnumFloorFillEntryState.NONE
        }
        if not held:
            return {}
        return {
            f"floor:{v.line_id}:{v.task_id}": v
            for v in self._tickets.entry_ticket_verdicts(held)
        }

    def _candidates_request(
        self,
        ctx: _Pass,
        floor_text: str,
        payloads: tuple[ModelFloorTaskPayloadVerdict, ...],
        ticket_verdicts: tuple[ModelFloorEntryTicketVerdict, ...],
    ) -> ModelFloorFillCandidatesRequest:
        entries = tuple(
            ModelFloorFillStateEntry(
                key=e.key,
                status=e.state.value,
                at=(
                    e.reserved_at
                    if e.state is EnumFloorFillEntryState.RESERVED
                    else e.sent_at
                ).isoformat(),  # type: ignore[union-attr]
                ended=e.ended,
            )
            for e in ctx.state.entries.values()
            if e.state is not EnumFloorFillEntryState.NONE
        )
        return ModelFloorFillCandidatesRequest(
            now=ctx.now.isoformat(),
            floor_record=floor_text,
            max_age_minutes=self._config.max_age_minutes,
            max_fill_lanes_per_line=self._config.max_fill_lanes_per_line,
            sent_window_minutes=self._config.sent_window_minutes,
            payload_verdicts=payloads,
            ticket_verdicts=ticket_verdicts,
            entries=entries,
        )

    def _verdicts(
        self,
        ctx: _Pass,
        floor_text: str,
        payloads: tuple[ModelFloorTaskPayloadVerdict, ...],
    ) -> ModelFloorFillCandidatesResult:
        known = dict(ctx.ticket_verdicts)
        for entry in ctx.state.entries.values():
            if entry.ticket is not None and entry.key not in known:
                line_id, task_id = line_and_task(entry.key)
                known[entry.key] = ModelFloorEntryTicketVerdict(
                    line_id=line_id,
                    task_id=task_id,
                    status="usable",
                    ticket=entry.ticket,
                )
        return self._candidates.handle(
            self._candidates_request(ctx, floor_text, payloads, tuple(known.values()))
        )

    # S5

    def _send(
        self,
        ctx: _Pass,
        verdicts: ModelFloorFillCandidatesResult,
        only_key: str | None,
    ) -> None:
        if only_key is not None:
            self._send_one(ctx, verdicts, only_key)
            return
        for line in verdicts.lines:
            if line.verdict not in ("STARTABLE", "NEEDS-TICKET"):
                continue
            for candidate in line.candidates:
                if ctx.started >= ctx.free_slots:
                    return
                outcome = self._start_candidate(
                    ctx, candidate.key, candidate.verdict, candidate.ticket
                )
                if outcome in ("line-full", "start-failed"):
                    # A failed start ends the line's walk: the cause is the runner's, not the task's.
                    break
                if outcome == "no-slot":
                    return

    def _send_one(
        self, ctx: _Pass, verdicts: ModelFloorFillCandidatesResult, key: str
    ) -> None:
        match = None
        for line in verdicts.lines:
            for candidate in line.candidates:
                if candidate.key == key:
                    match = candidate
        if match is None:
            self._refuse(ctx, key, self._why_refused(ctx, verdicts, key))
            return
        if ctx.started >= ctx.free_slots:
            self._refuse(ctx, key, "no-free-slots")
            return
        self._start_candidate(ctx, key, match.verdict, match.ticket)

    def _why_refused(
        self, ctx: _Pass, verdicts: ModelFloorFillCandidatesResult, key: str
    ) -> str:
        entry = ctx.state.entries.get(key)
        if entry is not None and entry.state is not EnumFloorFillEntryState.NONE:
            return f"held:{entry.state.value}"
        try:
            line_id, task_id = line_and_task(key)
        except ValueError:
            return "not-a-floor-key"
        for line in verdicts.lines:
            if line.line_id == line_id:
                for item in line.untickettable:
                    if item.key == key:
                        return item.cause
                for task in line.not_ready:
                    if task.task_id == task_id:
                        return f"not-ready:{task.cause}"
                return f"line:{line.verdict}"
        return "unknown-line"

    def _refuse(self, ctx: _Pass, key: str, cause: str) -> None:
        ctx.results.append(
            ModelFloorFillKeyResult(key=key, kind="refused", cause=cause)
        )

    def _start_candidate(self, ctx: _Pass, key: str, verdict: str, ticket: str) -> str:
        """Mint if needed, reserve (a write), then start. Returns ``ok``, ``line-full``, ``no-slot``."""
        if verdict == "NEEDS-TICKET":
            minted = self._tickets.mint(key)
            if minted.state != "ticket" or not minted.ticket:
                ctx.results.append(
                    ModelFloorFillKeyResult(
                        key=key,
                        kind="mint-refused",
                        cause=minted.cause or "mint-unavailable",
                    )
                )
                return "ok"
            ticket = minted.ticket
            stored = self._reduce(
                ctx.state, ModelFloorFillTicketStored(key=key, ticket=ticket)
            )
            if stored.dropped_reason is not None:
                return "ok"
            self._write(ctx, stored.state)
        reserve = ModelFloorFillReserveRequested(
            key=key,
            max_fill_lanes_per_line=self._config.max_fill_lanes_per_line,
            slot_budget=ctx.free_slots - ctx.started,
            landing_only=False,
            stop_signal=False,
            sent_window_minutes=self._config.sent_window_minutes,
            now=self._clock.now(),
        )
        reserved = self._reduce(ctx.state, reserve)
        if reserved.dropped_reason is not None:
            reason = reserved.dropped_reason
            if reason is EnumFloorFillDropReason.CAPACITY:
                return "line-full"
            if reason is EnumFloorFillDropReason.NO_SLOT:
                return "no-slot"
            return "ok"
        # The reserve write comes before the intent it authorises.
        self._write(ctx, reserved.state)
        (intent,) = reserved.intents
        outcome = self._starter.start(
            ModelLaneStart(
                key=intent.key,
                lane=intent.lane,
                ticket=intent.ticket,
                attempt=intent.attempt,
                argv=_fill(
                    self._config.runner_command,
                    {
                        "lane": intent.lane,
                        "key": intent.key,
                        "ticket": intent.ticket,
                        "attempt": str(intent.attempt),
                    },
                ),
                environment=tuple(sorted(self._config.process_environment.items())),
                working_directory=self._config.process_working_directory,
            )
        )
        if outcome.state == "started":
            ctx.started += 1
            ctx.results.append(
                ModelFloorFillKeyResult(
                    key=key, kind="started", lane=intent.lane, ticket=intent.ticket
                )
            )
            return "ok"
        failed = self._reduce(
            ctx.state,
            ModelFloorFillStartFailed(
                key=key, attempt=intent.attempt, lane=intent.lane
            ),
        )
        if failed.dropped_reason is None:
            self._write(ctx, failed.state)
        ctx.results.append(
            ModelFloorFillKeyResult(
                key=key,
                kind="failed",
                lane=intent.lane,
                ticket=intent.ticket,
                cause=outcome.cause or "start-failed",
            )
        )
        return "start-failed"

    # ------------------------------------------------------------------ reads and writes

    def _reduce(
        self, state: ModelFloorFillState, observation: ModelFloorFillObservation
    ) -> ModelFloorFillReduceOutput:
        return self._reducer.handle(
            ModelFloorFillReduceInput(state=state, observation=observation)
        )

    def _write(self, ctx: _Pass, next_state: ModelFloorFillState) -> None:
        """One conditional write of the row. A lost race ends the command; nothing is retried."""
        landed = self._store.compare_and_set(
            self._config.scope,
            ctx.state.seq,
            next_state.seq,
            next_state.model_dump_json(),
        )
        if not landed:
            raise StaleStateError
        ctx.state = next_state
        ctx.writes += 1

    # ------------------------------------------------------------------ S6 and the answers

    def _finish(
        self,
        command: EnumFloorFillCommand,
        ctx: _Pass,
        *,
        hold: str,
        floor: ModelFloorFillCandidatesResult | None,
    ) -> ModelFloorFillAnswer:
        request = self._lines_request(ctx, hold, floor)
        rendered = self._lines.handle(request).rendered()
        lines = list(rendered)
        status = EnumFloorFillStatus.DEGRADED if ctx.unread else EnumFloorFillStatus.OK
        for result in ctx.results:
            if command is EnumFloorFillCommand.SEND_KEY:
                if result.kind == "started":
                    lines.append(f"FLOOR-SENT key={result.key} lane={result.lane}")
                else:
                    lines.append(f"FLOOR-REFUSED key={result.key} cause={result.cause}")
                    status = EnumFloorFillStatus.FAILED
        if command is EnumFloorFillCommand.SEND_KEY and not ctx.results:
            lines.append("FLOOR-REFUSED key=none cause=nothing-to-send")
            status = EnumFloorFillStatus.FAILED
        answer = ModelFloorFillAnswer(
            command=command,
            status=status,
            lines=tuple(lines),
            key_results=tuple(ctx.results),
            exit_code=EXIT_CODE_BY_STATUS[status],
            state_seq=ctx.state.seq,
            observed_at=ctx.now,
        )
        if command is EnumFloorFillCommand.TICK:
            return self._publish(ctx, answer)
        return answer

    def _publish(
        self, ctx: _Pass, answer: ModelFloorFillAnswer
    ) -> ModelFloorFillAnswer:
        """Store the tick's answer: conditional, and never from a lower state sequence than the stored one."""
        key = self._config.scope + ANSWER_ROW_SUFFIX
        try:
            read = self._store.load(key)
            if read.payload is not None:
                try:
                    stored = ModelFloorFillStoredAnswer.model_validate_json(
                        read.payload
                    )
                except (ValidationError, ValueError):
                    return self._failed(
                        answer.command, "UNKNOWN fill-state:answer-unparseable", answer
                    )
                if ctx.state.seq < stored.state_seq:
                    return self._failed(answer.command, LINE_STALE_STATE, answer)
            payload = ModelFloorFillStoredAnswer(
                lines=answer.lines,
                status=answer.status,
                observed_at=ctx.now,
                seq=read.version + 1,
                state_seq=ctx.state.seq,
            ).model_dump_json()
            if not self._store.compare_and_set(
                key, read.version, read.version + 1, payload
            ):
                return self._failed(answer.command, LINE_STALE_STATE, answer)
        except FloorFillUnreadableError as error:
            return self._failed(
                answer.command, f"UNKNOWN fill-state:{error.cause}", answer
            )
        return answer

    def _lines_request(
        self,
        ctx: _Pass,
        hold: str,
        floor: ModelFloorFillCandidatesResult | None,
    ) -> ModelFloorFillLinesRequest:
        lines: list[ModelFloorFillLinesLine] = []
        if floor is not None and floor.verdict == "READ":
            lines = [_lines_line(line) for line in floor.lines]
        entries = tuple(
            ModelFloorFillLinesEntry(
                key=e.key,
                line_id=_line_of(e.key),
                state=EnumFloorFillLinesEntryState(e.state.value),
                lane=e.lane,
                ended=e.ended,
                reserved_at=e.reserved_at,
            )
            for e in ctx.state.entries.values()
        )
        unresolved = tuple(
            e.key
            for e in ctx.state.entries.values()
            if ctx.ledger_unread and e.state is EnumFloorFillEntryState.RESERVED
        )
        start_results = tuple(
            ModelFloorFillLinesStartResult(
                key=r.key,
                kind=EnumFloorFillLinesStartKind(r.kind),
                lane=r.lane if r.kind == "started" else None,
                cause=r.cause if r.kind != "started" else None,
            )
            for r in ctx.results
            if r.kind in ("started", "failed", "mint-refused")
        )
        return ModelFloorFillLinesRequest(
            unread=tuple(ctx.unread),
            lines=tuple(lines),
            entries=entries,
            now=ctx.now,
            reserved_timeout_seconds=self._config.reserved_timeout_seconds,
            unresolved_keys=unresolved,
            start_results=start_results,
            free_slots_after=max(0, ctx.free_slots - ctx.started),
            hold=hold,
        )

    def _stale(self, command: EnumFloorFillCommand, ctx: _Pass) -> ModelFloorFillAnswer:
        return self._failed(
            command,
            LINE_STALE_STATE,
            ModelFloorFillAnswer(
                command=command,
                status=EnumFloorFillStatus.FAILED,
                key_results=tuple(ctx.results),
                exit_code=EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED],
                state_seq=ctx.state.seq,
                observed_at=ctx.now,
            ),
        )

    def _failed(
        self,
        command: EnumFloorFillCommand,
        line: str,
        base: ModelFloorFillAnswer | None = None,
    ) -> ModelFloorFillAnswer:
        """A pass that did not complete: the one line, ``FAILED``, and the starts earlier writes had committed."""
        results = base.key_results if base is not None else ()
        return ModelFloorFillAnswer(
            command=command,
            status=EnumFloorFillStatus.FAILED,
            lines=(line,),
            key_results=results,
            exit_code=EXIT_CODE_BY_STATUS[EnumFloorFillStatus.FAILED],
            state_seq=base.state_seq if base is not None else 0,
            observed_at=base.observed_at if base is not None else None,
        )

    # ------------------------------------------------------------------ read-only commands

    def _lines_command(self) -> ModelFloorFillAnswer:
        """Print the stored answer of the last tick, judged by its own ``observed_at``."""
        read = self._store.load(self._config.scope + ANSWER_ROW_SUFFIX)
        now = self._clock.now()
        if read.payload is None:
            return self._degraded(
                EnumFloorFillCommand.LINES, "UNKNOWN floor-fill-answer-absent", now
            )
        try:
            stored = ModelFloorFillStoredAnswer.model_validate_json(read.payload)
        except (ValidationError, ValueError):
            raise FloorFillUnreadableError("answer-unparseable") from None
        age = now - stored.observed_at
        if age > timedelta(minutes=self._config.max_age_minutes):
            minutes = int(age.total_seconds() // 60)
            return self._degraded(
                EnumFloorFillCommand.LINES,
                f"UNKNOWN floor-fill-answer-stale:{minutes}m",
                now,
            )
        return ModelFloorFillAnswer(
            command=EnumFloorFillCommand.LINES,
            status=stored.status,
            lines=stored.lines,
            exit_code=EXIT_CODE_BY_STATUS[stored.status],
            state_seq=stored.state_seq,
            observed_at=stored.observed_at,
        )

    @staticmethod
    def _degraded(
        command: EnumFloorFillCommand, line: str, now: datetime
    ) -> ModelFloorFillAnswer:
        return ModelFloorFillAnswer(
            command=command,
            status=EnumFloorFillStatus.DEGRADED,
            lines=(line,),
            exit_code=EXIT_CODE_BY_STATUS[EnumFloorFillStatus.DEGRADED],
            observed_at=now,
        )

    def _inspect(self) -> ModelFloorFillAnswer:
        """Read-only: T1's verdict for every line, the row's ``seq`` and the observation time."""
        state = self._read_state()
        ctx = _Pass(state, self._clock.now())
        floor_text = self._read_facts(ctx)
        if floor_text is None:
            return self._degraded(
                EnumFloorFillCommand.INSPECT,
                f"UNKNOWN floor-unread:{_unread_cause(ctx.unread, 'floor')}",
                ctx.now,
            )
        plan_texts = self._plan_texts(ctx, floor_text)
        payloads = tuple(self._tickets.payload_verdicts(floor_text, plan_texts))
        verdicts = self._verdicts(ctx, floor_text, payloads)
        if verdicts.verdict == "FLOOR-UNREAD":
            return self._degraded(
                EnumFloorFillCommand.INSPECT,
                f"UNKNOWN floor-unread:{verdicts.unread_cause}",
                ctx.now,
            )
        out = [
            "INSPECT "
            + json.dumps(
                {
                    "line": line.line_id,
                    "verdict": line.verdict,
                    "next": line.candidates[0].task_id if line.candidates else None,
                    "capacity_left": line.capacity_left,
                },
                sort_keys=True,
            )
            for line in verdicts.lines
        ]
        out.append(f"INSPECT seq={state.seq} observed_at={ctx.now.isoformat()}")
        return ModelFloorFillAnswer(
            command=EnumFloorFillCommand.INSPECT,
            status=EnumFloorFillStatus.OK,
            lines=tuple(out),
            exit_code=EXIT_CODE_BY_STATUS[EnumFloorFillStatus.OK],
            state_seq=state.seq,
            observed_at=ctx.now,
        )


def _cause(error: BaseException) -> str:
    if isinstance(error, FloorFillUnreadableError):
        return error.cause
    return type(error).__name__


def _unread_cause(unread: list[str], source: str) -> str:
    for item in unread:
        head, _, cause = item.partition(":")
        if head == source:
            return cause
    return "unknown"


def _lines_line(line: ModelFloorFillLine) -> ModelFloorFillLinesLine:
    verdict = EnumFloorFillLinesVerdict(line.verdict)
    if verdict in (
        EnumFloorFillLinesVerdict.STARTABLE,
        EnumFloorFillLinesVerdict.NEEDS_TICKET,
    ):
        first = line.candidates[0]
        return ModelFloorFillLinesLine(
            line_id=line.line_id,
            verdict=verdict,
            next_task_id=first.task_id,
            ticket=first.ticket or None,
        )
    if verdict is EnumFloorFillLinesVerdict.UNTICKETABLE:
        return ModelFloorFillLinesLine(
            line_id=line.line_id,
            verdict=verdict,
            untickable=tuple(
                ModelFloorFillLinesUntickable(task_id=u.task_id, cause=u.cause)
                for u in line.untickettable
            ),
        )
    if verdict is EnumFloorFillLinesVerdict.NO_STARTABLE:
        cause = (
            "; ".join(f"{n.task_id}={n.cause}" for n in line.not_ready) or "no-tasks"
        )
        return ModelFloorFillLinesLine(
            line_id=line.line_id, verdict=verdict, cause=cause
        )
    return ModelFloorFillLinesLine(line_id=line.line_id, verdict=verdict)


__all__: list[str] = [
    "ANSWER_ROW_SUFFIX",
    "FLOOR_TASK_CLASS",
    "FloorFillOrchestrator",
    "StaleStateError",
]
