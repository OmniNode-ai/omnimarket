# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dropped-work audit (OMN-26205): ledger rows in, the items with no live owner out.

Pure: the rows, the rules overlay, the merged pull requests, the admission refusals and a clock come in on the request;
nothing is read or written here. Six kinds of item, each with a stable id derived from its source row:

    ruling_no_mechanism   an operator RULING or OPERATOR-CONSENT row with no later row naming it or its ticket
    promise_no_lane       a SENT-ON-BEHALF promise with no later CLAIM, MSG or TERMINAL naming it
    blocked_next_step     a TERMINAL blocked, partial or failed that names a next step, with no later live CLAIM on its
                          ticket and no later closing row
    hold_unreleased       a HOLD whose pull request merged and that has no RELEASE
    claim_unfinished      a CLAIM whose lane wrote no row for the claim window and wrote no TERMINAL (a CLAIM the lane
                          closed with a TERMINAL is no item)
    admission_refusal     a refusal of admission of work a standing RULING covers

An item has a live owner when a live CLAIM (its lane wrote a row inside the window and no TERMINAL since) names its
ticket or pull request, or a later TERMINAL, RELEASE or ACK closes it. An item with no live owner yields one owner-missing
event and, once it has been unowned for the alarm delay, one alarm row. The audit assigns no owner and returns no
admission or refusal verdict.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid5

from omnimarket.models.dropped_work_audit import (
    EnumDroppedWorkKind,
    ModelDroppedWorkAlarmRow,
    ModelDroppedWorkAuditRequest,
    ModelDroppedWorkAuditResult,
    ModelDroppedWorkAuditRules,
    ModelDroppedWorkItem,
    ModelOwnerMissingEvent,
)
from omnimarket.models.model_reading import ModelLedgerEvidenceRow

_STAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
_SEPARATOR = " | "


def _stamp_text(value: datetime) -> str:
    return value.astimezone(UTC).strftime(_STAMP_FORMAT)


def _mentions(
    row: ModelLedgerEvidenceRow, needles: frozenset[str], ignored: tuple[str, ...]
) -> bool:
    """Whether a cell of ``row`` names one of the needles; a cell the rules list as ignored never counts."""
    for cell in row.raw.split(_SEPARATOR)[2:]:
        key = cell.partition("=")[0].strip()
        if key in ignored:
            continue
        if any(needle in cell for needle in needles):
            return True
    return False


class _Candidate:
    """One source row (or refusal) before its owner is judged."""

    def __init__(
        self,
        kind: EnumDroppedWorkKind,
        stamp: datetime,
        lane: str,
        row_type: str,
        ticket: str | None,
        pr: str | None,
        unowned_since: datetime,
    ) -> None:
        self.kind = kind
        self.stamp = stamp
        self.lane = lane
        self.row_type = row_type
        self.ticket = ticket
        self.pr = pr
        self.unowned_since = unowned_since

    def item_id(self) -> str:
        basis = "|".join(
            (
                self.kind.value,
                _stamp_text(self.stamp),
                self.row_type,
                self.lane,
                self.ticket or "",
                self.pr or "",
            )
        )
        return (
            f"dwa-{self.kind.value}-{hashlib.sha256(basis.encode()).hexdigest()[:12]}"
        )


class HandlerDroppedWorkAudit:
    """Find the ledger commitments with no live owner."""

    def handle(
        self, request: ModelDroppedWorkAuditRequest
    ) -> ModelDroppedWorkAuditResult:
        rules = request.rules
        now = request.now.astimezone(UTC)
        # the audit's own alarm rows are not a mechanism, a claim or an activity of a lane
        rows = tuple(
            sorted(
                (r for r in request.rows if r.lane != rules.audit_lane),
                key=lambda r: (r.stamp, r.raw),
            )
        )
        window = timedelta(seconds=rules.claim_window_seconds)
        floor = now - timedelta(seconds=rules.lookback_seconds)
        lane_last: dict[str, datetime] = {}
        for row in rows:
            if row.lane is not None and (
                row.lane not in lane_last or row.stamp > lane_last[row.lane]
            ):
                lane_last[row.lane] = row.stamp

        def later(source: _Candidate) -> list[ModelLedgerEvidenceRow]:
            return [r for r in rows if r.stamp > source.stamp]

        def claim_live(claim: ModelLedgerEvidenceRow) -> bool:
            lane = claim.lane or ""
            if lane_last.get(lane, claim.stamp) < now - window:
                return False
            return not any(
                r.row_type == rules.terminal_row_type
                and r.lane == claim.lane
                and r.stamp > claim.stamp
                for r in rows
            )

        def live_claim_on(
            source: _Candidate, after: datetime
        ) -> ModelLedgerEvidenceRow | None:
            for row in rows:
                if row.row_type != rules.claim_row_type or row.stamp <= after:
                    continue
                names_ticket = (
                    source.ticket is not None
                    and row.cells.get(rules.ticket_cell) == source.ticket
                )
                names_pr = source.pr is not None and source.pr in row.raw
                if (names_ticket or names_pr) and claim_live(row):
                    return row
            return None

        def closer(
            source: _Candidate, needles: frozenset[str], types: tuple[str, ...]
        ) -> ModelLedgerEvidenceRow | None:
            for row in later(source):
                if row.row_type in types and _mentions(
                    row, needles, rules.ignored_ref_cells
                ):
                    return row
            return None

        def later_terminal_on_ticket(
            source: _Candidate,
        ) -> ModelLedgerEvidenceRow | None:
            if source.ticket is None:
                return None
            for row in later(source):
                if (
                    row.row_type == rules.terminal_row_type
                    and row.cells.get(rules.ticket_cell) == source.ticket
                ):
                    return row
            return None

        def terminal_closes(claim: ModelLedgerEvidenceRow) -> bool:
            """Whether the claim's lane wrote a TERMINAL after it, or a TERMINAL names it as the CLAIM it closes."""
            return any(
                r.row_type == rules.terminal_row_type
                and r.stamp > claim.stamp
                and (
                    r.lane == claim.lane
                    or r.cells.get(rules.closes_claim_cell) == _stamp_text(claim.stamp)
                )
                for r in rows
            )

        candidates: list[tuple[_Candidate, str]] = []

        def judge(
            source: _Candidate, needles: frozenset[str], kind: EnumDroppedWorkKind
        ) -> str:
            """The reason the item has an owner, or the empty string."""
            if kind is EnumDroppedWorkKind.RULING_NO_MECHANISM:
                for row in later(source):
                    if row.row_type in rules.mechanism_row_types and (
                        (
                            source.ticket is not None
                            and row.cells.get(rules.ticket_cell) == source.ticket
                        )
                        or _mentions(row, needles, rules.ignored_ref_cells)
                    ):
                        return f"mechanism:{row.row_type}@{_stamp_text(row.stamp)}"
                return ""
            if kind is EnumDroppedWorkKind.PROMISE_NO_LANE:
                found = closer(
                    source,
                    needles,
                    tuple(
                        dict.fromkeys(
                            (
                                *rules.closing_row_types,
                                *rules.promise_followup_row_types,
                            )
                        )
                    ),
                )
                if found is not None:
                    return f"followup:{found.row_type}@{_stamp_text(found.stamp)}"
                claim = live_claim_on(source, source.stamp)
                return f"live-claim:{claim.lane}" if claim is not None else ""
            if kind is EnumDroppedWorkKind.BLOCKED_NEXT_STEP:
                claim = live_claim_on(source, source.stamp)
                if claim is not None:
                    return f"live-claim:{claim.lane}"
                found = closer(source, needles, rules.closing_row_types)
                if found is not None:
                    return f"closed:{found.row_type}@{_stamp_text(found.stamp)}"
                nxt = later_terminal_on_ticket(source)
                return f"superseded:{_stamp_text(nxt.stamp)}" if nxt else ""
            if kind is EnumDroppedWorkKind.HOLD_UNRELEASED:
                found = closer(source, needles, rules.closing_row_types)
                return (
                    f"released:{found.row_type}@{_stamp_text(found.stamp)}"
                    if found is not None
                    else ""
                )
            if kind is EnumDroppedWorkKind.CLAIM_UNFINISHED:
                claim = live_claim_on(source, source.stamp)
                if claim is not None and claim.lane != source.lane:
                    return f"taken-over:{claim.lane}"
                return ""
            # admission refusal: only a later non-terminal closing row naming the candidate owns it
            found = closer(
                source,
                needles,
                tuple(
                    t for t in rules.closing_row_types if t != rules.terminal_row_type
                ),
            )
            return (
                f"closed:{found.row_type}@{_stamp_text(found.stamp)}" if found else ""
            )

        kinds = frozenset(rules.kinds)
        pr_pattern = re.compile(rules.pr_ref_pattern)

        for row in rows:
            ticket = row.cells.get(rules.ticket_cell)
            lane = row.lane or row.cells.get("from", "")
            in_window = row.stamp >= floor
            if not in_window:
                continue
            stamp_needle = _stamp_text(row.stamp)
            if (
                EnumDroppedWorkKind.RULING_NO_MECHANISM in kinds
                and row.row_type in rules.ruling_row_types
            ):
                src = _Candidate(
                    EnumDroppedWorkKind.RULING_NO_MECHANISM,
                    row.stamp,
                    lane,
                    row.row_type,
                    ticket,
                    None,
                    row.stamp,
                )
                candidates.append(
                    (src, judge(src, frozenset({stamp_needle}), src.kind))
                )
            if (
                EnumDroppedWorkKind.PROMISE_NO_LANE in kinds
                and row.row_type == rules.promise_row_type
                and rules.promise_marker in row.raw
            ):
                needles = {stamp_needle}
                ident = row.cells.get(rules.hold_id_cell)
                if ident:
                    needles.add(ident)
                src = _Candidate(
                    EnumDroppedWorkKind.PROMISE_NO_LANE,
                    row.stamp,
                    lane,
                    row.row_type,
                    ticket,
                    None,
                    row.stamp,
                )
                candidates.append((src, judge(src, frozenset(needles), src.kind)))
            if (
                EnumDroppedWorkKind.BLOCKED_NEXT_STEP in kinds
                and row.row_type == rules.terminal_row_type
                and row.cells.get(rules.outcome_cell) in rules.blocked_outcomes
                and any(row.cells.get(cell) for cell in rules.next_step_cells)
            ):
                src = _Candidate(
                    EnumDroppedWorkKind.BLOCKED_NEXT_STEP,
                    row.stamp,
                    lane,
                    row.row_type,
                    ticket,
                    row.cells.get(rules.pr_cell),
                    row.stamp,
                )
                candidates.append(
                    (src, judge(src, frozenset({stamp_needle}), src.kind))
                )
            if (
                EnumDroppedWorkKind.HOLD_UNRELEASED in kinds
                and row.row_type == rules.hold_row_type
            ):
                merged = sorted(
                    (request.merged_prs[ref], ref)
                    for ref in {m.lower() for m in pr_pattern.findall(row.raw)}
                    if ref in request.merged_prs
                )
                if merged:
                    merged_at, ref = merged[0]
                    needles = {stamp_needle}
                    ident = row.cells.get(rules.hold_id_cell)
                    if ident:
                        needles.add(ident)
                    src = _Candidate(
                        EnumDroppedWorkKind.HOLD_UNRELEASED,
                        row.stamp,
                        lane,
                        row.row_type,
                        ticket,
                        ref,
                        max(row.stamp, merged_at.astimezone(UTC)),
                    )
                    candidates.append((src, judge(src, frozenset(needles), src.kind)))
            if (
                EnumDroppedWorkKind.CLAIM_UNFINISHED in kinds
                and row.row_type == rules.claim_row_type
                and row.lane is not None
                and lane_last[row.lane] < now - window
                and not terminal_closes(row)
            ):
                src = _Candidate(
                    EnumDroppedWorkKind.CLAIM_UNFINISHED,
                    row.stamp,
                    lane,
                    row.row_type,
                    ticket,
                    row.cells.get(rules.pr_cell),
                    lane_last[row.lane] + window,
                )
                candidates.append(
                    (src, judge(src, frozenset({stamp_needle}), src.kind))
                )

        if EnumDroppedWorkKind.ADMISSION_REFUSAL in kinds:
            for refusal in request.admission_refusals:
                if refusal.observed_at < floor:
                    continue
                candidate_key = refusal.candidate.lower()
                names = {candidate_key} | (
                    {refusal.ticket} if refusal.ticket else set()
                )
                covered = False
                for ruling in rows:
                    if ruling.row_type not in rules.ruling_row_types:
                        continue
                    ruling_needle = frozenset({_stamp_text(ruling.stamp)})
                    if _mentions(
                        ruling, frozenset(names), rules.ignored_ref_cells
                    ) or any(
                        _mentions(r, frozenset(names), rules.ignored_ref_cells)
                        and _mentions(r, ruling_needle, rules.ignored_ref_cells)
                        for r in rows
                    ):
                        covered = True
                        break
                if not covered:
                    continue
                src = _Candidate(
                    EnumDroppedWorkKind.ADMISSION_REFUSAL,
                    refusal.observed_at.astimezone(UTC),
                    "admission",
                    "refusal",
                    refusal.ticket,
                    refusal.candidate,
                    refusal.observed_at.astimezone(UTC),
                )
                candidates.append(
                    (src, judge(src, frozenset({candidate_key}), src.kind))
                )

        order = {kind: index for index, kind in enumerate(EnumDroppedWorkKind)}
        candidates.sort(key=lambda c: (order[c[0].kind], c[0].stamp, c[0].lane))
        items: list[ModelDroppedWorkItem] = []
        events: list[ModelOwnerMissingEvent] = []
        alarms: list[ModelDroppedWorkAlarmRow] = []
        delay = timedelta(seconds=rules.alarm_delay_seconds)
        for source, reason in candidates:
            item_id = source.item_id()
            step = rules.suggested_steps[source.kind]
            item = ModelDroppedWorkItem(
                item_id=item_id,
                kind=source.kind,
                source_stamp=source.stamp,
                source_lane=source.lane,
                ticket=source.ticket,
                pr=source.pr,
                unowned_since=source.unowned_since,
                owned=bool(reason),
                owner_reason=reason,
                suggested_step=step,
            )
            items.append(item)
            if item.owned:
                continue
            if item_id not in request.evented_items:
                events.append(
                    ModelOwnerMissingEvent(
                        event_id=item_id,
                        kind=source.kind,
                        source_stamp=source.stamp,
                        source_lane=source.lane,
                        ticket=source.ticket,
                        pr=source.pr,
                        unowned_since=source.unowned_since,
                        observed_at=now,
                        suggested_step=step,
                    )
                )
            if item_id not in request.alarmed_items and now >= (
                source.unowned_since + delay
            ):
                alarms.append(_alarm_row(rules, item, now))
        counts = {kind.value: 0 for kind in rules.kinds}
        for item in items:
            counts[item.kind.value] += 1
        return ModelDroppedWorkAuditResult(
            now=now,
            items=tuple(items),
            counts_by_kind=counts,
            unowned=sum(1 for i in items if not i.owned),
            events=tuple(events),
            alarms=tuple(alarms),
        )


def _alarm_row(
    rules: ModelDroppedWorkAuditRules, item: ModelDroppedWorkItem, now: datetime
) -> ModelDroppedWorkAlarmRow:
    """The STATUS alarm row in the shape of the red-main alarm's: ``alarm=``, the item, the source and the step."""
    request_id = str(uuid5(NAMESPACE_URL, f"onex:dropped-work-audit:{item.item_id}"))
    cells = [
        f"lane={rules.audit_lane}",
        f"alarm={rules.alarm_value}",
        f"kind={item.kind.value}",
        f"item={item.item_id}",
        f"source={_stamp_text(item.source_stamp)}/{item.source_lane}",
        f"unowned_since={_stamp_text(item.unowned_since)}",
    ]
    if item.ticket:
        cells.append(f"ticket={item.ticket}")
    if item.pr:
        cells.append(f"pr={item.pr}")
    cells.append(f"step={item.suggested_step}")
    cells.append(f"req={request_id}")
    row = _SEPARATOR.join(
        [
            _stamp_text(now),
            rules.alarm_row_type,
            *cells,
            f"{item.kind.value} has no live owner",
        ]
    )
    return ModelDroppedWorkAlarmRow(
        item_id=item.item_id, request_id=request_id, row=row
    )


__all__: list[str] = ["HandlerDroppedWorkAudit"]
