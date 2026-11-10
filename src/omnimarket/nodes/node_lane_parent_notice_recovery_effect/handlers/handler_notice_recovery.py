# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Consume the parent notices of lanes that ended, with a durable cursor, and recover a failed placement once (OMN-23389).

2026-11-09: the overlay store repair lane failed placement at 20:28Z (status node-unavailable). The orchestrator's
lane read listed it as running for an hour, and its parent notice, a ledger row to the orchestrator, went unread.

* ``read_lane_states`` is the lane read: a lane with a CLAIM and no TERMINAL reads ``running`` unless a later notice
  of a failure status (the lane never ran) says it did not, and then it reads ``failed`` with the notice's status.
* ``HandlerNoticeRecovery`` consumes each notice to the parent once. The cursor (the last notice's time and the
  hashes of the notices at that time) is durable and moves only after the notice's acknowledgement and, for a failed
  placement, after the recovery lane's start is recorded; a restart between the two replays the notice and the
  recorded recovery lane name stops a second lane.
* A failed placement starts a recovery lane through a placement path the overlay names, not the node that failed.
  Every recovery carries the original lane's attempt family (``<lane>-r<n>``), a retry budget and a backoff from the
  overlay; at the budget one escalation row is written and no more lanes are started.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import yaml

from omnimarket.handlers.node_overlay_reader import load_structured_overlay
from omnimarket.nodes.node_lane_parent_notice_recovery_effect.handlers.adapters import (
    ArgvNoticeSink,
    ArgvRecoveryDispatcher,
    FileReceipts,
    InMemoryLedgerLines,
    InMemoryNoticeSink,
    InMemoryNoticeStateStore,
    InMemoryReceipts,
    InMemoryRecoveryDispatcher,
    JsonFileNoticeStateStore,
    RecoveryError,
)
from omnimarket.nodes.node_lane_parent_notice_recovery_effect.models.model_notice_recovery import (
    EnumLaneReadState,
    EnumNoticeAction,
    ModelLaneRead,
    ModelNoticeConsumeRequest,
    ModelNoticeConsumeResult,
    ModelNoticeFamily,
    ModelNoticeOutcome,
    ModelNoticeRecovered,
    ModelNoticeRecoveryOverlay,
    ModelNoticeState,
)
from omnimarket.nodes.node_lane_parent_notice_recovery_effect.protocols import (
    ProtocolLedgerLines,
    ProtocolNoticeSink,
    ProtocolNoticeStateStore,
    ProtocolReceiptReader,
    ProtocolRecoveryDispatcher,
)

NODE_NAME = "node_lane_parent_notice_recovery_effect"
OVERLAY_ENV = "ONEX_LANE_NOTICE_RECOVERY_OVERLAY"
STAMP = "%Y-%m-%dT%H:%M:%SZ"
FAMILY_SUFFIX = re.compile(r"-r\d+$")


class NoticeRefusalError(ValueError):
    """The overlay is unreadable or a time is not a time."""


def load_overlay(overlay_file: str = "") -> ModelNoticeRecoveryOverlay:
    if overlay_file:
        try:
            document = yaml.safe_load(Path(overlay_file).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise NoticeRefusalError(
                f"overlay {overlay_file} is unreadable ({type(exc).__name__})"
            ) from None
        return load_structured_overlay(
            NODE_NAME, ModelNoticeRecoveryOverlay, explicit=document
        )
    return load_structured_overlay(
        NODE_NAME, ModelNoticeRecoveryOverlay, file_env=OVERLAY_ENV
    )


def parse_stamp(value: str) -> datetime:
    try:
        return datetime.strptime(value, STAMP).replace(tzinfo=UTC)
    except ValueError:
        raise NoticeRefusalError(f"{value!r} is not a {STAMP} time") from None


def family_root(lane: str) -> str:
    return FAMILY_SUFFIX.sub("", lane)


@dataclass(frozen=True)
class Row:
    """One ledger row: its time, kind, key=value cells and the hash that names it."""

    line: str
    ts: str
    kind: str
    cells: dict[str, str]

    @property
    def ident(self) -> str:
        return hashlib.sha256(self.line.encode("utf-8")).hexdigest()[:16]


def parse_row(line: str) -> Row | None:
    parts = [p.strip() for p in line.split(" | ")]
    if len(parts) < 3 or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", parts[0]):
        return None
    cells: dict[str, str] = {}
    for cell in parts[2:]:
        name, eq, value = cell.partition("=")
        if eq and name and " " not in name:
            cells.setdefault(name, value)
    return Row(line=line, ts=parts[0], kind=parts[1], cells=cells)


def read_lane_states(
    lines: list[str], failure_statuses: tuple[str, ...]
) -> tuple[ModelLaneRead, ...]:
    """The lane read: CLAIM opens a lane, TERMINAL ends it, a later failure notice says it never ran."""
    state: dict[str, ModelLaneRead] = {}
    for line in lines:
        row = parse_row(line)
        lane = row.cells.get("lane", "") if row else ""
        if row is None or not lane:
            continue
        if row.kind == "CLAIM":
            state[lane] = ModelLaneRead(lane=lane, state=EnumLaneReadState.RUNNING)
        elif row.kind == "TERMINAL":
            state[lane] = ModelLaneRead(
                lane=lane,
                state=EnumLaneReadState.ENDED,
                reason=row.cells.get("outcome", ""),
            )
        elif (
            row.kind in ("STATUS", "MSG")
            and row.cells.get("status") in failure_statuses
            and "run" in row.cells
            and lane in state
            and state[lane].state is EnumLaneReadState.RUNNING
        ):
            state[lane] = ModelLaneRead(
                lane=lane,
                state=EnumLaneReadState.FAILED,
                reason=f"placement {row.cells['status']} (notice {row.ts})",
            )
    return tuple(state.values())


class HandlerNoticeRecovery:
    """One pass over the parent's notices. Ports are injected: the defaults keep everything in memory."""

    def __init__(
        self,
        *,
        ledger: ProtocolLedgerLines | None = None,
        store: ProtocolNoticeStateStore | None = None,
        receipts: ProtocolReceiptReader | None = None,
        dispatcher: ProtocolRecoveryDispatcher | None = None,
        sink: ProtocolNoticeSink | None = None,
        overlay: ModelNoticeRecoveryOverlay | None = None,
        home: Path | None = None,
        real: bool = False,
    ) -> None:
        self._ledger = ledger
        self._store = store
        self._receipts = receipts
        self._dispatcher = dispatcher
        self._sink = sink
        self._overlay = overlay
        self._home = home if home is not None else Path.home()
        self._real = real

    def _ports(
        self, overlay: ModelNoticeRecoveryOverlay
    ) -> tuple[
        ProtocolLedgerLines,
        ProtocolNoticeStateStore,
        ProtocolReceiptReader,
        ProtocolRecoveryDispatcher,
        ProtocolNoticeSink,
    ]:
        if self._real:
            state_file = overlay.state_file
            path = (
                self._home / state_file[len("$HOME/") :]
                if state_file.startswith("$HOME/")
                else Path(state_file)
            )
            if self._ledger is None:
                raise NoticeRefusalError("a real pass needs the ledger it reads")
            return (
                self._ledger,
                self._store or JsonFileNoticeStateStore(path),
                self._receipts or FileReceipts(),
                self._dispatcher
                or ArgvRecoveryDispatcher(
                    overlay.recovery_argv, overlay.recovery_timeout_seconds
                ),
                self._sink
                or ArgvNoticeSink(
                    overlay.ack_argv,
                    overlay.escalate_argv,
                    overlay.recovery_timeout_seconds,
                ),
            )
        return (
            self._ledger or InMemoryLedgerLines(),
            self._store or InMemoryNoticeStateStore(),
            self._receipts or InMemoryReceipts(),
            self._dispatcher or InMemoryRecoveryDispatcher(),
            self._sink or InMemoryNoticeSink(),
        )

    # ------------------------------------------------------------------ pass

    def handle(self, request: ModelNoticeConsumeRequest) -> ModelNoticeConsumeResult:
        overlay = self._overlay or load_overlay(request.overlay_file)
        ledger, store, receipts, dispatcher, sink = self._ports(overlay)
        now = parse_stamp(request.now) if request.now else datetime.now(UTC)
        lines = ledger.read_lines()
        state = store.load()
        notices = sorted(
            (
                row
                for row in map(parse_row, lines)
                if row is not None
                and row.kind in ("STATUS", "MSG")
                and row.cells.get("to") == overlay.parent_lane
                and "status" in row.cells
                and "run" in row.cells
                and "lane" in row.cells
            ),
            key=lambda r: r.ts,
        )
        outcomes: list[ModelNoticeOutcome] = []
        stopped = False
        for row in notices:
            if row.ts < state.cursor_ts or (
                row.ts == state.cursor_ts and row.ident in state.seen
            ):
                continue
            state, outcome, stop = self._consume(
                row,
                state,
                overlay,
                now,
                store,
                receipts,
                dispatcher,
                sink,
                request.ledger_file,
            )
            outcomes.append(outcome)
            if stop:
                stopped = True
                break
            state = self._advance(state, row)
            store.save(state)
        return ModelNoticeConsumeResult(
            outcomes=tuple(outcomes),
            stopped=stopped,
            cursor_ts=state.cursor_ts,
            lanes=read_lane_states(lines, overlay.failure_statuses),
        )

    @staticmethod
    def _advance(state: ModelNoticeState, row: Row) -> ModelNoticeState:
        if row.ts > state.cursor_ts:
            return state.model_copy(update={"cursor_ts": row.ts, "seen": (row.ident,)})
        return state.model_copy(update={"seen": (*state.seen, row.ident)})

    def _consume(
        self,
        row: Row,
        state: ModelNoticeState,
        overlay: ModelNoticeRecoveryOverlay,
        now: datetime,
        store: ProtocolNoticeStateStore,
        receipts: ProtocolReceiptReader,
        dispatcher: ProtocolRecoveryDispatcher,
        sink: ProtocolNoticeSink,
        ledger_file: str,
    ) -> tuple[ModelNoticeState, ModelNoticeOutcome, bool]:
        lane = row.cells["lane"]
        status = row.cells["status"]
        root = family_root(lane)

        def outcome(
            action: EnumNoticeAction, recovery_lane: str = "", reason: str = ""
        ) -> ModelNoticeOutcome:
            return ModelNoticeOutcome(
                notice=row.ts,
                lane=lane,
                status=status,
                action=action,
                recovery_lane=recovery_lane,
                reason=reason,
            )

        def finish(
            current: ModelNoticeState, result: ModelNoticeOutcome
        ) -> tuple[ModelNoticeState, ModelNoticeOutcome, bool]:
            try:
                sink.acknowledge(lane, row.ts, result.action.value)
            except RecoveryError as exc:
                held = outcome(
                    EnumNoticeAction.DEFERRED, reason=f"acknowledgement: {exc}"
                )
                return current, held, True
            return current, result, False

        def escalate(
            current: ModelNoticeFamily, why: str
        ) -> tuple[ModelNoticeState, ModelNoticeOutcome, bool]:
            try:
                sink.escalate(
                    f"lane {root} could not be recovered: {why}; notice {row.ts} status {status}; "
                    f"budget {overlay.retry_budget}"
                )
            except RecoveryError as exc:
                held = outcome(EnumNoticeAction.DEFERRED, reason=f"escalation: {exc}")
                return state, held, True
            marked = state.model_copy(
                update={
                    "families": {
                        **state.families,
                        root: current.model_copy(update={"escalated": True}),
                    }
                }
            )
            store.save(marked)
            return finish(marked, outcome(EnumNoticeAction.ESCALATED, reason=why))

        if status not in overlay.failure_statuses:
            return finish(state, outcome(EnumNoticeAction.ACKNOWLEDGED))

        recovered = state.recovered.get(row.ident)
        family = state.families.get(root)
        if recovered is not None and recovered.dispatched:
            return finish(
                state, outcome(EnumNoticeAction.ALREADY_RECOVERED, recovered.lane)
            )
        if recovered is None:
            if family is not None and family.escalated:
                return finish(state, outcome(EnumNoticeAction.ESCALATED_ALREADY))
            if family is not None and family.attempts >= overlay.retry_budget:
                return escalate(
                    family,
                    f"{family.attempts} recovery attempt(s) of {root} also failed placement",
                )
            if family is not None:
                wait = overlay.backoff_seconds[
                    min(family.attempts, len(overlay.backoff_seconds)) - 1
                ]
                due = parse_stamp(family.last_recovery_at).timestamp() + wait
                if now.timestamp() < due:
                    return (
                        state,
                        outcome(
                            EnumNoticeAction.DEFERRED,
                            reason=f"recovery {family.attempts} of {root} stands for {int(due - now.timestamp())} s more",
                        ),
                        True,
                    )
            receipt = receipts.read(row.cells.get("receipt", ""))
            brief = receipt.get("brief") if receipt else None
            if not isinstance(brief, str) or not brief:
                return escalate(
                    family
                    or ModelNoticeFamily(
                        attempts=0, last_recovery_at=now.strftime(STAMP)
                    ),
                    "the receipt the notice names holds no brief to recover from",
                )
            attempts = (family.attempts if family else 0) + 1
            recovered = ModelNoticeRecovered(lane=f"{root}-r{attempts}")
            state = state.model_copy(
                update={
                    "families": {
                        **state.families,
                        root: ModelNoticeFamily(
                            attempts=attempts, last_recovery_at=now.strftime(STAMP)
                        ),
                    },
                    "recovered": {**state.recovered, row.ident: recovered},
                }
            )
            store.save(state)
        if family is not None and family.escalated:
            return finish(state, outcome(EnumNoticeAction.ESCALATED_ALREADY))
        receipt = receipts.read(row.cells.get("receipt", ""))
        brief_file = str(receipt.get("brief", "")) if receipt else ""
        model = str(receipt.get("model", "sonnet")) if receipt else "sonnet"
        try:
            dispatcher.dispatch(
                recovered.lane,
                lane,
                brief_file,
                row.cells.get("ticket", ""),
                overlay.parent_lane,
                model,
                ledger_file,
            )
        except RecoveryError as exc:
            failed = recovered.model_copy(update={"failures": recovered.failures + 1})
            state = state.model_copy(
                update={"recovered": {**state.recovered, row.ident: failed}}
            )
            store.save(state)
            if failed.failures >= overlay.retry_budget:
                return escalate(
                    state.families[root],
                    f"the recovery start failed {failed.failures} time(s): {exc}",
                )
            return (
                state,
                outcome(EnumNoticeAction.DISPATCH_FAILED, recovered.lane, str(exc)),
                True,
            )
        done = recovered.model_copy(update={"dispatched": True})
        state = state.model_copy(
            update={"recovered": {**state.recovered, row.ident: done}}
        )
        store.save(state)
        return finish(state, outcome(EnumNoticeAction.RECOVERED, done.lane))
