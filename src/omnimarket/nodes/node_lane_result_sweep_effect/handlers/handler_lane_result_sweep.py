# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Classify every lane receipt by its result and hold the class to the one-hour rule (OMN-22967).

Rebuild plan 3.6 row 13 and W17: 13,826 of 20,112 clear results were failures while the TERMINAL
rows said handed-off. The sweep reads each lane TERMINAL and takes the worse of the claimed
``outcome`` and the lane's own result (``lane_result`` / ``result``, ``blocker``, ``engine_rc``),
so a hand-off that stopped on a blocker reads blocked and never done. Each receipt then gets a
class row. A class that is not a clean finish gets an action row when the error rule table has a
rule for its normalized class key. Inside one hour of its class row, such a receipt needs an
action row or a valid park (a citation and an unexpired ``review_by``). Past the hour with neither,
or with a park past its ``review_by``, the reading is NOT WORKING.

It writes class rows, action rows and one reading row, never a gate, a stop or an admission.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from omnimarket.models.error_normalizer import (
    ModelErrorClassNormalizeRequest,
    ModelErrorEvent,
)
from omnimarket.models.model_lane_result_sweep import (
    EnumLaneResultClass,
    EnumSweepState,
    ModelLaneReceipt,
    ModelLaneResultSweepCommand,
    ModelLaneResultSweepResult,
    ModelReceiptVerdict,
)
from omnimarket.models.model_reading import (
    EnumReadingOutcome,
    EnumReadingVerdict,
    ModelLedgerEvidenceRow,
)
from omnimarket.nodes.node_error_class_normalize_compute.handlers.handler_error_class_normalize import (
    HandlerErrorClassNormalize,
)
from omnimarket.nodes.node_lane_result_sweep_effect.overlay import (
    ModelLaneResultSweepOverlay,
)
from omnimarket.nodes.node_reading_compute.handlers.handler_reading_compute import (
    stamp_text,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ProtocolLedgerRowSource,
    ProtocolReadingAppender,
)

ACTION_CLASS = "lane-result-class"
ACTION_ACTION = "lane-result-action"
ACTION_PARK = "lane-result-park"
ACTION_READING = "lane-result-reading"
_ROW_TYPE = "STATUS"
_SRC = "src=lane-result-sweep"
_EMPTY = {"", "none", "-", "n/a", "null"}
_TEXT_LIMIT = 500


def _cell(row: ModelLedgerEvidenceRow, name: str) -> str:
    value = row.cells.get(name, "").strip()
    return "" if value.lower() in _EMPTY else value


def _clean(text: str) -> str:
    return " ".join(text.replace("|", "/").split())


def _closing_text(row: ModelLedgerEvidenceRow) -> str:
    last = row.raw.rsplit(" | ", 1)[-1]
    return "" if "=" in last.split(" ", 1)[0] else last[:_TEXT_LIMIT]


def receipt_of(row: ModelLedgerEvidenceRow) -> ModelLaneReceipt | None:
    """One TERMINAL row as a receipt, or None when it names no lane or outcome."""
    lane = _cell(row, "lane")
    outcome = _cell(row, "outcome")
    if not lane or not outcome:
        return None
    rc = _cell(row, "engine_rc")
    blocker = _clean(_cell(row, "blocker"))
    return ModelLaneReceipt(
        lane=lane,
        ticket=_cell(row, "ticket"),
        finished_at=row.stamp,
        terminal_outcome=outcome,
        result_outcome=_cell(row, "lane_result") or _cell(row, "result"),
        blocker=blocker,
        engine_rc=int(rc) if rc.lstrip("-").isdigit() else None,
        error_text=blocker
        or _clean(_cell(row, "reason"))
        or (
            f"result {result}"
            if (result := (_cell(row, "lane_result") or _cell(row, "result")))
            else ""
        )
        or _clean(_closing_text(row)),
    )


def classify(
    receipt: ModelLaneReceipt,
    outcome_classes: Mapping[str, EnumLaneResultClass],
    result_classes: Mapping[str, EnumLaneResultClass],
) -> EnumLaneResultClass:
    """The worst class any signal of the receipt gives (AC1).

    The claimed ``outcome`` and the lane's own ``result`` are each read through the overlay's
    vocabulary (an unlisted token is unknown, so a new word shows instead of passing as done). A
    blocker reads blocked and a non-zero engine exit reads failed whatever was claimed, so a
    ``handed-off`` TERMINAL that stopped on a blocker is blocked and never done.
    """
    signals = [
        outcome_classes.get(
            receipt.terminal_outcome.lower(), EnumLaneResultClass.UNKNOWN
        )
    ]
    if receipt.result_outcome:
        signals.append(
            result_classes.get(
                receipt.result_outcome.lower(), EnumLaneResultClass.UNKNOWN
            )
        )
    if receipt.blocker:
        signals.append(EnumLaneResultClass.BLOCKED)
    if receipt.engine_rc not in (None, 0):
        signals.append(EnumLaneResultClass.FAILED)
    return max(signals, key=lambda member: member.severity)


@dataclass
class _Existing:
    """What the sweep's own rows already say about one receipt."""

    class_at: datetime | None = None
    acted: bool = False
    parks: list[tuple[str, datetime]] = field(default_factory=list)


def _parse_time(text: str) -> datetime | None:
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def fold_existing(rows: tuple[ModelLedgerEvidenceRow, ...]) -> dict[str, _Existing]:
    """Fold the sweep rows by receipt. A park counts only with a citation and a review time."""
    folded: dict[str, _Existing] = {}
    for row in rows:
        key = _cell(row, "receipt")
        action = _cell(row, "action")
        if not key or not action.startswith("lane-result-"):
            continue
        state = folded.setdefault(key, _Existing())
        if action == ACTION_CLASS:
            if state.class_at is None or row.stamp < state.class_at:
                state.class_at = row.stamp
        elif action == ACTION_ACTION:
            state.acted = True
        elif action == ACTION_PARK:
            cite = _cell(row, "cite")
            review_by = _parse_time(_cell(row, "review_by"))
            if cite and review_by is not None:
                state.parks.append((cite, review_by))
    return folded


class HandlerLaneResultSweep:
    """``handle(ModelLaneResultSweepCommand) -> ModelLaneResultSweepResult``."""

    def __init__(
        self,
        *,
        overlay: ModelLaneResultSweepOverlay,
        ledger: ProtocolLedgerRowSource,
        appender: ProtocolReadingAppender | None,
        clock: Callable[[], datetime],
    ) -> None:
        self._overlay = overlay
        self._ledger = ledger
        self._appender = appender
        self._clock = clock
        self._normalizer = HandlerErrorClassNormalize()

    # ---- reading -------------------------------------------------------------------------

    def handle(
        self, command: ModelLaneResultSweepCommand
    ) -> ModelLaneResultSweepResult:
        if command.write and self._appender is None:
            return self._failed(command, "no-appender-for-a-writing-run")
        now = self._clock().astimezone(UTC)
        until = now + timedelta(seconds=1)
        terminals = self._ledger.read(
            ModelLedgerQuery(since=command.since, until=until, row_type="TERMINAL")
        )
        sweep_rows = self._ledger.read(
            ModelLedgerQuery(
                since=command.since,
                until=until,
                row_type=_ROW_TYPE,
                term="action=lane-result-",
            )
        )
        errors = [r.error for r in (terminals, sweep_rows) if r.error]
        if errors:
            return self._unreadable(command, now, errors)

        existing = fold_existing(sweep_rows.rows)
        receipts = [
            receipt
            for row in terminals.rows
            if (receipt := receipt_of(row)) is not None
        ]
        deadline = timedelta(seconds=self._overlay.publish.action_deadline_seconds)
        verdicts: list[ModelReceiptVerdict] = []
        written: list[str] = []
        for receipt in sorted(receipts, key=lambda r: (r.finished_at, r.lane)):
            verdict = self._judge(
                receipt, existing.get(receipt.receipt_key), now, deadline
            )
            if command.write:
                try:
                    verdict, rows = self._write_rows(receipt, verdict, existing, now)
                except RuntimeError as exc:
                    # An append that did not land stops the run; the rows already
                    # appended stay named, and the reading is not written over a gap.
                    return ModelLaneResultSweepResult(
                        correlation_id=command.correlation_id,
                        status="failed",
                        rows_written=tuple(written),
                        failure=str(exc),
                    )
                written.extend(rows)
            verdicts.append(verdict)

        reasons = tuple(f"{v.state.value}:{v.lane}" for v in verdicts if v.state.fails)
        result = ModelLaneResultSweepResult(
            correlation_id=command.correlation_id,
            status="completed",
            verdict=EnumReadingVerdict.NOT_WORKING
            if reasons
            else EnumReadingVerdict.WORKING,
            outcome=EnumReadingOutcome.READ,
            reasons=reasons,
            verdicts=tuple(verdicts),
            rows_written=tuple(written),
        )
        return self._write_reading(command, result, now) if command.write else result

    def _classify(self, receipt: ModelLaneReceipt) -> EnumLaneResultClass:
        return classify(
            receipt, self._overlay.outcome_classes, self._overlay.result_classes
        )

    def _classify_text(self, receipt: ModelLaneReceipt) -> tuple[str, str, str]:
        """Class key, rule id and action kind for a receipt, from the error rule table."""
        text = receipt.error_text or f"lane result {self._classify(receipt).value}"
        try:
            key = self._normalizer.handle(
                ModelErrorClassNormalizeRequest(
                    event=ModelErrorEvent(
                        source=self._overlay.source,
                        producer=self._overlay.producer,
                        text=text,
                    ),
                    facts=self._overlay.normalizer_facts,
                )
            ).class_key
        except ValueError:
            return "", "", ""
        for rule in self._overlay.error_rule_document.get("error_rules", []):
            if key in rule.get("class_keys", []):
                action = rule.get("action") or {}
                return key, str(rule.get("id", "")), str(action.get("kind", ""))
        return key, "", ""

    def _judge(
        self,
        receipt: ModelLaneReceipt,
        existing: _Existing | None,
        now: datetime,
        deadline: timedelta,
    ) -> ModelReceiptVerdict:
        effective = self._classify(receipt)
        class_key, rule_id = "", ""
        if effective.needs_action:
            class_key, rule_id, _ = self._classify_text(receipt)
        reasons: list[str] = []
        if receipt.terminal_outcome.lower() != effective.value:
            reasons.append(f"claimed-{receipt.terminal_outcome.lower()}")
        unclassified = existing is None or existing.class_at is None
        join = self._overlay.join_j12
        # J12: every typed receipt, clean or not, has its class row inside the join window.
        join_late = (
            unclassified
            and join is not None
            and now - receipt.finished_at
            > timedelta(seconds=join.class_row_deadline_seconds)
        )
        if join_late:
            state = EnumSweepState.UNCLASSIFIED_LATE
        elif not effective.needs_action:
            state = EnumSweepState.CLEAR
        elif existing is None or existing.class_at is None:
            late = now - receipt.finished_at >= deadline
            state = EnumSweepState.UNCLASSIFIED_LATE if late else EnumSweepState.PENDING
        elif existing.acted:
            state = EnumSweepState.ACTED
        elif any(review_by > now for _, review_by in existing.parks):
            state = EnumSweepState.PARKED
        elif existing.parks:
            state = EnumSweepState.PARK_EXPIRED
        elif now - existing.class_at >= deadline:
            state = EnumSweepState.NO_ACTION_AT_ONE_HOUR
        else:
            state = EnumSweepState.PENDING
        return ModelReceiptVerdict(
            lane=receipt.lane,
            ticket=receipt.ticket,
            receipt_key=receipt.receipt_key,
            terminal_outcome=receipt.terminal_outcome,
            effective_class=effective,
            state=state,
            class_key=class_key,
            rule_id=rule_id,
            reasons=tuple(reasons),
        )

    # ---- writing -------------------------------------------------------------------------

    def _cells(self, action: str, receipt: ModelLaneReceipt, *more: str) -> str:
        publish = self._overlay.publish
        cells = [
            f"lane={publish.lane}",
            _SRC,
            f"actor={publish.actor}",
            "model=none",
            f"piece={publish.piece}",
            f"action={action}",
            f"receipt={receipt.receipt_key}",
            f"receipt_lane={receipt.lane}",
            *([f"ticket={receipt.ticket}"] if receipt.ticket else []),
            *more,
        ]
        return " | ".join(cells)

    def _append(self, kind: str, receipt_key: str, now: datetime, cells: str) -> str:
        assert self._appender is not None
        row = f"{stamp_text(now)} | {_ROW_TYPE} | {cells}"
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{kind}|{receipt_key}"))
        outcome = self._appender.append(row + "\n", request_id)
        if not outcome.ok:
            raise RuntimeError(f"append-failed:{outcome.status}")
        return row

    def _write_rows(
        self,
        receipt: ModelLaneReceipt,
        verdict: ModelReceiptVerdict,
        existing: dict[str, _Existing],
        now: datetime,
    ) -> tuple[ModelReceiptVerdict, list[str]]:
        """Write the class row, and the action row a matching rule gives; return the new state."""
        rows: list[str] = []
        state = existing.get(receipt.receipt_key)
        if state is not None and state.class_at is not None:
            return verdict, rows
        _, rule_id, action_kind = (
            self._classify_text(receipt)
            if verdict.effective_class.needs_action
            else ("", "", "")
        )
        rows.append(
            self._append(
                ACTION_CLASS,
                receipt.receipt_key,
                now,
                self._cells(
                    ACTION_CLASS,
                    receipt,
                    f"terminal={receipt.terminal_outcome}",
                    f"effective={verdict.effective_class.value}",
                    f"class_key={verdict.class_key or 'none'}",
                    f"rule={rule_id or 'none'}",
                    f"overlay_hash={self._overlay.overlay_hash()}",
                ),
            )
        )
        acted = bool(rule_id) and verdict.effective_class.needs_action
        if acted:
            mode = next(
                (
                    str(r.get("mode", ""))
                    for r in self._overlay.error_rule_document.get("error_rules", [])
                    if r.get("id") == rule_id
                ),
                "",
            )
            rows.append(
                self._append(
                    ACTION_ACTION,
                    receipt.receipt_key,
                    now,
                    self._cells(
                        ACTION_ACTION,
                        receipt,
                        f"rule={rule_id}",
                        f"act={action_kind or 'none'}",
                        f"mode={mode or 'none'}",
                    ),
                )
            )
        new_state = verdict.state
        if verdict.state is EnumSweepState.PENDING and acted:
            new_state = EnumSweepState.ACTED
        elif verdict.state is EnumSweepState.UNCLASSIFIED_LATE:
            new_state = EnumSweepState.UNCLASSIFIED_LATE
        existing[receipt.receipt_key] = _Existing(class_at=now, acted=acted)
        return verdict.model_copy(update={"state": new_state}), rows

    def _write_reading(
        self,
        command: ModelLaneResultSweepCommand,
        result: ModelLaneResultSweepResult,
        now: datetime,
    ) -> ModelLaneResultSweepResult:
        assert result.verdict is not None
        assert result.outcome is not None
        failing = [v for v in result.verdicts if v.state.fails]
        content = hashlib.sha256(
            "|".join([result.verdict.value, *result.reasons, stamp_text(now)]).encode(
                "utf-8"
            )
        ).hexdigest()[:24]
        publish = self._overlay.publish
        cells = " | ".join(
            [
                f"lane={publish.lane}",
                _SRC,
                f"actor={publish.actor}",
                "model=none",
                f"piece={publish.piece}",
                f"action={ACTION_READING}",
                f"verdict={result.verdict.value}",
                f"outcome={result.outcome.value}",
                f"reason={','.join(result.reasons) or 'none'}",
                f"receipts={len(result.verdicts)}",
                f"failing={len(failing)}",
                f"deadline={publish.action_deadline_seconds}",
                f"content={content}",
                f"overlay_hash={self._overlay.overlay_hash()}",
            ]
        )
        try:
            row = self._append(ACTION_READING, content, now, cells)
        except RuntimeError as exc:
            return result.model_copy(update={"status": "failed", "failure": str(exc)})
        return result.model_copy(update={"rows_written": (*result.rows_written, row)})

    # ---- failures ------------------------------------------------------------------------

    def _failed(
        self, command: ModelLaneResultSweepCommand, why: str
    ) -> ModelLaneResultSweepResult:
        return ModelLaneResultSweepResult(
            correlation_id=command.correlation_id, status="failed", failure=why
        )

    def _unreadable(
        self,
        command: ModelLaneResultSweepCommand,
        now: datetime,
        errors: list[str],
    ) -> ModelLaneResultSweepResult:
        result = ModelLaneResultSweepResult(
            correlation_id=command.correlation_id,
            status="completed",
            verdict=EnumReadingVerdict.NOT_WORKING,
            outcome=EnumReadingOutcome.SOURCE_UNREADABLE,
            reasons=tuple(f"ledger-unreadable:{_clean(e or '')}" for e in errors),
        )
        return self._write_reading(command, result, now) if command.write else result


__all__ = [
    "ACTION_ACTION",
    "ACTION_CLASS",
    "ACTION_PARK",
    "ACTION_READING",
    "HandlerLaneResultSweep",
    "classify",
    "fold_existing",
    "receipt_of",
]
