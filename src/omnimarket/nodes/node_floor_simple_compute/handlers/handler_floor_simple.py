# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Build the simple floor from readings (OMN-19998, 17a; plan section 10 and figure M1).

Pure: ``handle(ModelFloorSimpleInput) -> ModelFloorSimple``. One sentence, three numbers, causes biggest first, the
rebuild strip, the 18 piece rows with their joins and reading ages, the lab-state lines, and the rule check. Every
number carries the read it came from, so a checker can repeat it; an input that was not read is drawn as unread and
becomes a cause of its own, never an empty or zero value.

The colour rule (M1): a cause draws red only when no live lane is on it or it needs the operator; otherwise it draws
amber and names the lanes. A stoppage whose own record says it is not occurring (no row since, past the overlay's
window) and that needs nobody draws ``quiet``: listed with its age, last in order, not counted red. ``check_red_rule``
recomputes all of that from the finished document and the lanes alone, so a builder that gets the rule wrong is caught
by a reader that does not share its code path.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from omnimarket.models.floor_17a import (
    ModelFloorCause,
    ModelFloorCauseIn,
    ModelFloorJoinRow,
    ModelFloorLabLine,
    ModelFloorLane,
    ModelFloorNumber,
    ModelFloorPieceRow,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelJoinReceipt,
    ModelLabSourceVerdict,
    ModelRebuild,
    ModelRedRule,
)


def _ago(seconds: int | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 120:
        return f"{seconds} s ago"
    if seconds < 7200:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h ago"
    return f"{seconds // 86400} d ago"


def _cause_draw(cause: ModelFloorCause | None) -> str:
    """How a row that stands on a cause draws: red with no cause or a red one, grey when the cause is quiet."""
    if cause is None or cause.state == "red":
        return "red"
    return "grey" if cause.state == "quiet" else "amber"


def live_lanes(
    lanes: tuple[ModelFloorLane, ...],
    owners: tuple[str, ...],
    prs: tuple[str, ...] = (),
    cause_id: str = "",
) -> tuple[str, ...]:
    """The lanes that are really on a cause: not stalled, and not dead or finished by the process reading.

    A lane is on a cause when its CLAIM or a STATUS row says ``owns=floor:<cause id>``, or it works on one of the
    cause's pull requests, or its ticket is one the overlay or a lab source declares as owning the cause. A ticket that merely appears among the places a problem was seen is not
    ownership: many unrelated lanes share an umbrella ticket, so matching on it names lanes that are not on the cause.
    """
    wanted_owners = set(owners)
    wanted_prs = set(prs)
    return tuple(
        sorted(
            lane.lane
            for lane in lanes
            if not lane.stalled
            and lane.liveness not in {"dead", "finished"}
            and (
                (lane.ticket and lane.ticket in wanted_owners)
                or (lane.pr and lane.pr in wanted_prs)
                or (cause_id and cause_id in lane.owns)
            )
        )
    )


def check_red_rule(
    document: ModelFloorSimple, lanes: tuple[ModelFloorLane, ...]
) -> ModelRedRule:
    """The reading of the colour rule, recomputed from the document and the lanes only."""
    violations: list[str] = []
    with_work = 0
    red_rows = sum(1 for c in document.causes if c.listed and c.state == "red")
    known = {c.id: c for c in document.causes}
    for cause in document.causes:
        if cause.members:
            states = {known[m].state for m in cause.members if m in known}
            expected = "red" if "red" in states else "amber"
            if cause.state != expected:
                violations.append(
                    f"{cause.id} draws {cause.state} but its members say {expected}"
                )
            continue
        on_it = live_lanes(lanes, cause.owners, cause.prs, cause.id)
        under_way = bool(on_it)
        if cause.state == "red" and under_way and not cause.needs_operator:
            with_work += 1
            violations.append(f"{cause.id} draws red while {', '.join(on_it)} is on it")
        if (
            cause.state == "red"
            and cause.still_occurring is False
            and not cause.needs_operator
        ):
            violations.append(
                f"{cause.id} draws red though its record says it is not occurring (last seen {_ago(cause.last_seen_age_s)})"
            )
        if cause.state == "amber" and not under_way:
            violations.append(f"{cause.id} draws amber with no lane on it")
        if cause.state == "quiet" and (
            cause.still_occurring is not False or cause.needs_operator or under_way
        ):
            violations.append(
                f"{cause.id} draws quiet though it is occurring, needs you or has a lane on it"
            )
    rows: list[tuple[str, str | None, str]] = [
        *((f"piece {p.piece}", p.cause_id, p.draw) for p in document.pieces),
        *((f"lab {line.id}", line.cause_id, line.draw) for line in document.lab_state),
    ]
    for name, cause_id, draw in rows:
        if draw != "red":
            continue
        red_rows += 1
        owner = known.get(cause_id) if cause_id else None
        if owner is None:
            violations.append(f"{name} draws red with no cause behind it")
        elif owner.state != "red":
            violations.append(f"{name} draws red but its cause {owner.id} is amber")
        elif (
            live_lanes(lanes, owner.owners, owner.prs, owner.id)
            and not owner.needs_operator
        ):
            with_work += 1
            violations.append(f"{name} draws red with work under way on {owner.id}")
    return ModelRedRule.model_validate(
        {
            "state": "WORKING" if not violations else "NOT WORKING",
            "red_rows": red_rows,
            "red_with_work_under_way": with_work,
            "violations": tuple(violations),
        }
    )


class HandlerFloorSimple:
    """The simple floor, from readings."""

    def handle(self, request: ModelFloorSimpleInput) -> ModelFloorSimple:
        ov = request.overlay
        now = request.now
        lab = request.lab.verdicts if request.lab is not None else ()
        rows = self._piece_rows(request, lab)
        candidates = self._candidates(request, lab, rows)
        causes = self._causes(candidates, request)
        by_id = {c.id: c for c in causes}
        pieces = tuple(self._row_draw(r, by_id) for r in rows)
        lines = tuple(self._lab_line(v, by_id) for v in lab)
        level = self._level(causes)
        sentence, subline = self._words(causes)
        document = ModelFloorSimple.model_validate(
            {
                "generated_at": now,
                "freshness_max_age_s": ov.freshness_max_age_s,
                "level": level,
                "sentence": sentence,
                "subline": subline,
                "numbers": self._numbers(request),
                "causes": causes,
                "causes_shown": ov.causes.max_shown,
                "rebuild": self._rebuild(request, rows),
                "pieces": pieces,
                "lab_state": lines,
                "alarms": request.alarms,
                "red_rule": {
                    "state": "UNKNOWN",
                    "red_rows": 0,
                    "red_with_work_under_way": 0,
                },
                "chain_findings": (
                    request.receipts.findings if request.receipts is not None else ()
                ),
                "details_href": ov.details_href,
                "sources_read": request.sources_read,
                "alarm_heartbeat_at": request.alarm_heartbeat_at,
                "outside_goal": request.outside_goal,
            }
        )
        return document.model_copy(
            update={"red_rule": check_red_rule(document, request.lanes)}
        )

    @staticmethod
    def _level(causes: tuple[ModelFloorCause, ...]) -> str:
        """Red when a cause has nobody on it or needs you, amber when every live cause has a lane; quiet ones add nothing."""
        if any(c.state == "red" for c in causes):
            return "red"
        return "amber" if any(c.state == "amber" for c in causes) else "green"

    # --- the three numbers --------------------------------------------------------------------------------------------

    @staticmethod
    def _numbers(r: ModelFloorSimpleInput) -> tuple[ModelFloorNumber, ...]:
        n = r.overlay.numbers
        watcher_ok = r.watcher_read_at is not None and (
            (r.now - r.watcher_read_at).total_seconds() <= n.watcher_max_age_s
        )
        record_ok = r.record_read_at is not None and (
            (r.now - r.record_read_at).total_seconds() <= n.record_max_age_s
        )
        merges_ok = watcher_ok and r.merges_in_window is not None
        prs_ok = watcher_ok and r.open_prs is not None
        slots_ok = record_ok and r.slots_working is not None and r.slots_total
        if prs_ok and r.open_prs_before is not None:
            diff = (r.open_prs or 0) - r.open_prs_before
            trend = (
                "flat" if diff == 0 else (f"up {diff}" if diff > 0 else f"down {-diff}")
            )
            prs_label = f"open in-scope pull requests, {trend} over the last hour"
        else:
            prs_label = "open in-scope pull requests"
        slots_label = "lab slots working"
        if r.slots_unread_hosts:
            slots_label += f", {len(r.slots_unread_hosts)} hosts unread"
        return (
            ModelFloorNumber.model_validate(
                {
                    "key": "merges",
                    "value": str(r.merges_in_window) if merges_ok else "unread",
                    "label": f"in-scope merges in the last {n.merge_window_minutes} min, goal {n.landing_goal_per_hour} an hour"
                    if merges_ok
                    else "merges: the watcher or the goal scope is not read",
                    "basis": "merges of the goal's scope in the pull request watcher state with merged_at inside the window",
                    "read_at": r.watcher_read_at,
                    "read": merges_ok,
                }
            ),
            ModelFloorNumber.model_validate(
                {
                    "key": "open_prs",
                    "value": str(r.open_prs) if prs_ok else "unread",
                    "label": prs_label
                    if prs_ok
                    else "open pull requests: the watcher or the goal scope is not read",
                    "basis": "open pull requests of the goal's scope in the watcher state, held-excluded ones left out",
                    "read_at": r.watcher_read_at,
                    "read": prs_ok,
                }
            ),
            ModelFloorNumber.model_validate(
                {
                    "key": "slots",
                    "value": f"{r.slots_working} of {r.slots_total}"
                    if slots_ok
                    else "unread",
                    "label": slots_label
                    if slots_ok
                    else "lab slots: the floor record is not read",
                    "basis": "running and total slots of the read hosts in the floor record's bays",
                    "read_at": r.record_read_at,
                    "read": bool(slots_ok),
                }
            ),
        )

    # --- the piece rows -----------------------------------------------------------------------------------------------

    def _piece_rows(
        self, r: ModelFloorSimpleInput, lab: tuple[ModelLabSourceVerdict, ...]
    ) -> list[ModelFloorPieceRow]:
        ov = r.overlay
        receipts = (
            {p.piece: p for p in r.receipts.pieces} if r.receipts is not None else {}
        )
        joins = {j.join: j for j in r.receipts.joins} if r.receipts is not None else {}
        rows: list[ModelFloorPieceRow] = []
        working = self._working_pieces(r, lab)
        for definition in sorted(ov.pieces, key=lambda p: p.piece):
            n = definition.piece
            rc = receipts.get(n)
            go = r.go_given_through
            after = ov.rebuild.start_after.get(n, ())
            admitted = (
                (go is not None and n <= go)
                or n in ov.rebuild.step0_pieces
                or (bool(after) and all(p in working for p in after))
            )
            state, age, max_age, why = self._reading(
                r, n, definition.reading_max_age_s, lab
            )
            state, why, lab_cause = self._with_lab_sources(r, n, lab, state, why)
            started = rc is not None and rc.started
            unread_go = go is None and not admitted
            # The reading as the writer wrote it; the strip draws it only when the piece counts. A piece the go does not
            # cover, that has not started and reads UNKNOWN (the writer has no producer for it) has no reading to draw:
            # it is not started, never a failure and never a cause. A lab source the piece stands on still draws its own cause.
            written_state = state
            if state == "UNKNOWN" and not admitted and not started and not lab_cause:
                state = None
            if state is None:
                if started:
                    status, text = "started", "started; no reading row yet"
                elif admitted:
                    status, text = "unknown", "admitted; no golden and error event yet"
                elif unread_go:
                    status, text = (
                        "unknown",
                        "not read: the go and the events are on the ledger, which could not be read",
                    )
                else:
                    status, text = "not_started", "not started"
            else:
                status = {
                    "WORKING": "working",
                    "NOT WORKING": "not_working",
                    "UNKNOWN": "unknown",
                }[state]
                text = f"reading {state}" + (f": {why}" if why else "")
                if admitted and not started:
                    status = "unknown" if status == "working" else status
                    if rc is None or rc.last_emission_at is None:
                        text += "; no golden and error event yet"
            reason = text
            if rc is not None and rc.last_emission_at is not None:
                text += (
                    f" · golden {_ago(rc.golden_age_s)}, error {_ago(rc.error_age_s)}"
                    f"{' (stale)' if rc.freshness == 'stale' else ''}"
                )
            elif admitted:
                text += " · no events yet"
            rows.append(
                ModelFloorPieceRow.model_validate(
                    {
                        "piece": n,
                        "name": definition.name,
                        "status": status,
                        "draw": "grey" if status == "not_started" else "green",
                        "text": text,
                        "reading_state": written_state,
                        "reading_age_s": age,
                        "reading_max_age_s": max_age,
                        "cause_id": lab_cause
                        or (
                            "source:ledger"
                            if unread_go and state is None and not started
                            else None
                        ),
                        "golden_age_s": rc.golden_age_s if rc else None,
                        "error_age_s": rc.error_age_s if rc else None,
                        "freshness": rc.freshness if rc else "none",
                        "what": definition.what,
                        "reason": reason,
                        "next_text": self._next_text(status),
                        "window_s": ov.rebuild.event_window_s,
                        "golden_in_window": self._chains_in_window(
                            r, n, "golden", ov.rebuild.event_window_s
                        ),
                        "error_in_window": self._chains_in_window(
                            r, n, "error", ov.rebuild.event_window_s
                        ),
                        "joins": tuple(
                            self._join_row(j, joins.get(j.id), r.now)
                            for j in ov.joins
                            if j.from_piece == n
                        ),
                    }
                )
            )
        return rows

    @staticmethod
    def _chains_in_window(
        r: ModelFloorSimpleInput,
        piece: int,
        kind: Literal["golden", "error"],
        window_s: int,
    ) -> int | None:
        """How many chains of ``piece`` ended as ``kind`` inside the window; ``None`` when the receipts were not read."""
        if r.receipts is None:
            return None
        return sum(
            1
            for c in r.receipts.chains
            if c.piece == piece
            and c.kind == kind
            and c.ended_at is not None
            and 0 <= (r.now - c.ended_at).total_seconds() <= window_s
        )

    @staticmethod
    def _next_text(status: str) -> str:
        """The step a piece's state calls for, in words for the side panel."""
        if status == "not_started":
            return "Waits for the go. Nothing of it runs yet."
        if status in ("unknown", "started"):
            return "Needs a fresh reading and a golden and an error event from it."
        if status == "not_working":
            return "Clear what the reading names, then it reads working again."
        return "Measured against the old way for 7 days, then kept or rolled back."

    @staticmethod
    def _reading(
        r: ModelFloorSimpleInput,
        piece: int,
        default_max: int,
        lab: tuple[ModelLabSourceVerdict, ...],
    ) -> tuple[str | None, int | None, int, str]:
        """The reading of a piece: state, age, maximum age, and a reason. ``None`` state means no reading."""
        if piece == r.overlay.rebuild.lab_state_piece and r.lab is not None:
            bad = [v for v in lab if v.state != "WORKING"]
            ages = [v.age_s for v in lab if v.age_s is not None]
            max_age = min((v.max_age_s for v in lab), default=default_max)
            age = max(ages) if ages else None
            state = "NOT WORKING" if bad else "WORKING"
            why = f"{len(bad)} of {len(lab)} lab sources not working" if bad else ""
            return state, age, max_age, why
        row = (r.readings or {}).get(piece)
        if row is None:
            return None, None, default_max, ""
        age = max(int((r.now - row.probed_at).total_seconds()), 0)
        if age > row.max_age_s:
            return "NOT WORKING", age, row.max_age_s, f"stale, read {_ago(age)}"
        return row.state, age, row.max_age_s, row.reason

    @classmethod
    def _working_pieces(
        cls, r: ModelFloorSimpleInput, lab: tuple[ModelLabSourceVerdict, ...]
    ) -> frozenset[int]:
        """The pieces whose reading, with the lab sources they stand on folded in, reads WORKING now."""
        working: set[int] = set()
        for definition in r.overlay.pieces:
            state, _age, _max_age, why = cls._reading(
                r, definition.piece, definition.reading_max_age_s, lab
            )
            state, _why, _cause = cls._with_lab_sources(
                r, definition.piece, lab, state, why
            )
            if state == "WORKING":
                working.add(definition.piece)
        return frozenset(working)

    @staticmethod
    def _with_lab_sources(
        r: ModelFloorSimpleInput,
        piece: int,
        lab: tuple[ModelLabSourceVerdict, ...],
        state: str | None,
        why: str,
    ) -> tuple[str | None, str, str | None]:
        """Fold in the lab sources the overlay says the piece also stands on.

        A piece whose own reading covers only part of what its name says (the central test harness is the reading of the
        piece that also holds the CI runners) cannot draw working while a source it stands on is down or unread. Returns
        the state, the reason, and the cause of the first failing source.
        """
        wanted = r.overlay.rebuild.piece_lab_sources.get(piece, ())
        failing = [v for v in lab if v.id in wanted and v.state != "WORKING"]
        if not failing:
            return state, why, None
        unread = all(v.state == "UNKNOWN" for v in failing)
        note = "; ".join(
            f"{v.title} {'not read' if v.state == 'UNKNOWN' else 'not working'}"
            for v in failing
        )
        merged = f"{why}; {note}" if why else note
        if state is None or state == "WORKING":
            state = "UNKNOWN" if unread else "NOT WORKING"
        elif state == "UNKNOWN" and not unread:
            state = "NOT WORKING"
        return state, merged, f"lab:{failing[0].id}"

    @staticmethod
    def _join_row(
        definition: object, receipt: ModelJoinReceipt | None, now: datetime
    ) -> ModelFloorJoinRow:
        jid = getattr(definition, "id", "")
        label = getattr(definition, "label", "")
        to_piece = getattr(definition, "to_piece", 0)
        golden = error = None
        state = "unread"
        if receipt is not None:
            state = receipt.state
            if receipt.golden_at:
                golden = max(int((now - receipt.golden_at).total_seconds()), 0)
            if receipt.error_at:
                error = max(int((now - receipt.error_at).total_seconds()), 0)
        return ModelFloorJoinRow.model_validate(
            {
                "id": jid,
                "label": label,
                "to_piece": to_piece,
                "state": state,
                "golden_age_s": golden,
                "error_age_s": error,
            }
        )

    # --- causes -------------------------------------------------------------------------------------------------------

    def _candidates(
        self,
        r: ModelFloorSimpleInput,
        lab: tuple[ModelLabSourceVerdict, ...],
        rows: list[ModelFloorPieceRow],
    ) -> list[ModelFloorCauseIn]:
        ov = r.overlay.causes
        out: list[ModelFloorCauseIn] = []

        def add(
            cid: str,
            title: str,
            detail: str,
            size: int,
            unit: str,
            tickets: tuple[str, ...],
            *,
            owners: tuple[str, ...] = (),
            prs: tuple[str, ...] = (),
            last_seen: datetime | None = None,
        ) -> None:
            declared = tuple(dict.fromkeys((*owners, *ov.owners.get(cid, ()))))
            out.append(
                ModelFloorCauseIn.model_validate(
                    {
                        "id": cid,
                        "title": ov.titles.get(cid, title),
                        "detail": detail,
                        "size": size,
                        "size_unit": unit,
                        "tickets": tuple(dict.fromkeys((*tickets, *declared))),
                        "owners": declared,
                        "prs": prs,
                        "needs_operator": cid in ov.needs_operator,
                        "last_seen": last_seen,
                    }
                )
            )

        for cls, title in ov.pr_classes.items():
            count = r.pr_class_counts.get(cls, 0)
            if count:
                add(
                    f"prs:{cls}",
                    title,
                    f"Holding {count} pull requests.",
                    count,
                    "pull requests",
                    r.pr_class_tickets.get(cls, ()),
                    prs=r.pr_class_prs.get(cls, ()),
                )
        for item in r.stoppages:
            if item.id in ov.ignore_stoppages:
                continue
            add(
                item.id,
                item.title,
                self._stoppage_detail(item, r),
                item.size,
                item.size_unit,
                item.tickets,
                last_seen=item.last_seen,
            )
        for name, ok in sorted(r.sources_read.items()):
            if not ok:
                add(
                    f"source:{name}",
                    f"The floor cannot read its {name.replace('_', ' ')}",
                    "That input is drawn as unread, not as empty.",
                    1,
                    "inputs",
                    (),
                )
        lab_causes = 0
        for v in lab:
            if v.state == "WORKING":
                continue
            lab_causes += 1
            failing = sum(1 for s in v.subjects if not s.ok)
            kind = "is not being read" if v.state == "UNKNOWN" else "is not working"
            add(
                f"lab:{v.id}",
                f"Lab: {v.title} {kind}",
                v.reason,
                max(failing, 1),
                "lab readings",
                v.tickets,
                owners=v.tickets,
            )
        if r.receipts is not None and r.receipts.findings:
            add(
                "chains:broken",
                "Some emission chains break the contract",
                "; ".join(f"{f.subject}: {f.code}" for f in r.receipts.findings[:3]),
                len(r.receipts.findings),
                "broken chains",
                (),
            )
        undelivered = [a for a in r.alarms if a.delivery == "undelivered"]
        if undelivered:
            add(
                "alarm:undelivered",
                "A lab alarm has no delivery receipt",
                "; ".join(a.title for a in undelivered[:3]),
                len(undelivered),
                "alarms",
                (),
            )
        lab_piece = r.overlay.rebuild.lab_state_piece
        for row in rows:
            if (
                row.status in ("not_working", "unknown")
                and not row.cause_id
                and not (row.piece == lab_piece and lab_causes)
            ):
                add(
                    f"piece:{row.piece}",
                    f"Piece {row.piece}, {row.name}, is not working",
                    row.text,
                    1,
                    "pieces",
                    (),
                )
        return out

    @staticmethod
    def _stoppage_detail(item: ModelFloorCauseIn, r: ModelFloorSimpleInput) -> str:
        """What a stoppage row says: how often, how long ago, whether it is still occurring, and which cost is real.

        The count and last-seen time come from the friction rollup, which is built on a cadence of its own, so the row
        names when it was built. A logged cost that the overlay marks as an estimate of idle capacity is not shown as
        lane-minutes lost: nothing measured it.
        """
        ov = r.overlay.causes
        parts = [item.detail]
        if item.last_seen is not None:
            age = max(int((r.now - item.last_seen).total_seconds()), 0)
            seen = f"last seen {_ago(age)}"
            if ov.still_occurring_within_s is not None:
                seen += (
                    ", still occurring"
                    if age <= ov.still_occurring_within_s
                    else ", no row since in the rollup"
                )
            parts.append(seen)
        if item.id in ov.estimated_cost_stoppages:
            parts.append("no measured cost (the logged figure is an estimate)")
        elif item.reported_cost_minutes is not None:
            parts.append(f"{item.reported_cost_minutes:g} lane-minutes logged")
        text = ", ".join(parts) + "."
        if r.friction_as_of is not None:
            built = max(int((r.now - r.friction_as_of).total_seconds()), 0)
            text += f" From the friction rollup built {_ago(built)}; later rows are not in it."
        return text

    @staticmethod
    def _causes(
        candidates: list[ModelFloorCauseIn], r: ModelFloorSimpleInput
    ) -> tuple[ModelFloorCause, ...]:
        causes: list[ModelFloorCause] = []
        for c in candidates:
            lanes = live_lanes(r.lanes, c.owners, c.prs, c.id)
            red = c.needs_operator or not lanes
            age = (
                max(int((r.now - c.last_seen).total_seconds()), 0)
                if c.last_seen is not None
                else None
            )
            within = r.overlay.causes.still_occurring_within_s
            occurring = (
                age <= within if age is not None and within is not None else None
            )
            # A stoppage that stopped and needs nobody is a fact about the past: listed with its age, not red.
            state = (
                "quiet"
                if occurring is False and not c.needs_operator and not lanes
                else "red"
                if red
                else "amber"
            )
            causes.append(
                ModelFloorCause.model_validate(
                    {
                        "id": c.id,
                        "title": c.title,
                        "detail": c.detail,
                        "size": c.size,
                        "size_unit": c.size_unit,
                        "state": state,
                        "work_under_way": bool(lanes),
                        "needs_operator": c.needs_operator,
                        "lanes": lanes,
                        "tickets": c.tickets,
                        "owners": c.owners,
                        "prs": c.prs,
                        "last_seen": c.last_seen,
                        "last_seen_age_s": age,
                        "still_occurring": occurring,
                    }
                )
            )
        members = [c for c in causes if c.id.startswith("lab:")]
        if members:
            red = any(m.state == "red" for m in members)
            causes = [
                c.model_copy(update={"listed": False}) if c in members else c
                for c in causes
            ]
            causes.append(
                ModelFloorCause.model_validate(
                    {
                        "id": "lab:state",
                        "title": HandlerFloorSimple._lab_title(members, r),
                        "detail": "; ".join(
                            m.title.removeprefix("Lab: ") for m in members[:4]
                        )
                        + (
                            "."
                            if len(members) <= 4
                            else f"; and {len(members) - 4} more."
                        ),
                        "size": sum(m.size for m in members),
                        "size_unit": "lab readings",
                        "state": "red" if red else "amber",
                        "work_under_way": not red,
                        "needs_operator": any(m.needs_operator for m in members),
                        "lanes": tuple(sorted({x for m in members for x in m.lanes})),
                        "tickets": (),
                        "members": tuple(m.id for m in members),
                    }
                )
            )
        return tuple(sorted(causes, key=lambda c: (c.state == "quiet", -c.size, c.id)))

    @staticmethod
    def _lab_title(members: list[ModelFloorCause], r: ModelFloorSimpleInput) -> str:
        """Count the lab sources that fail, naming the ones that are only unread: unread is not the same as not working."""
        states = {v.id: v.state for v in r.lab.verdicts} if r.lab else {}
        total = len(states) or len(members)
        unread = sum(
            1 for m in members if states.get(m.id.removeprefix("lab:")) == "UNKNOWN"
        )
        down = len(members) - unread
        parts = [f"{down} not working"] if down else []
        if unread:
            parts.append(f"{unread} not read")
        return f"{len(members)} of {total} lab sources fail: {', '.join(parts)}"

    @staticmethod
    def _row_draw(
        row: ModelFloorPieceRow, by_id: dict[str, ModelFloorCause]
    ) -> ModelFloorPieceRow:
        if row.status in ("not_started", "started", "working"):
            return row
        cid = row.cause_id or f"piece:{row.piece}"
        if cid not in by_id:
            # The lab-state piece: its cause is its first failing lab source.
            cid = "lab:state" if "lab:state" in by_id else cid
        cause = by_id.get(cid)
        draw = _cause_draw(cause)
        return row.model_copy(update={"draw": draw, "cause_id": cid})

    @staticmethod
    def _lab_line(
        v: ModelLabSourceVerdict,
        by_id: dict[str, ModelFloorCause],
    ) -> ModelFloorLabLine:
        cause = by_id.get(f"lab:{v.id}")
        if v.state == "WORKING":
            draw, under_way, cid = "green", False, None
        else:
            draw = _cause_draw(cause)
            under_way = bool(cause and cause.work_under_way)
            cid = f"lab:{v.id}"
        text = v.summary if v.state == "WORKING" else f"{v.state}: {v.reason}"
        return ModelFloorLabLine.model_validate(
            {
                "id": v.id,
                "title": v.title,
                "state": v.state,
                "draw": draw,
                "text": text,
                "reading_age_s": v.age_s,
                "max_age_s": v.max_age_s,
                "work_under_way": under_way,
                "cause_id": cid,
            }
        )

    # --- words and the rebuild strip ----------------------------------------------------------------------------------

    @staticmethod
    def _words(causes: tuple[ModelFloorCause, ...]) -> tuple[str, str]:
        listed = tuple(c for c in causes if c.listed)
        quiet = [c for c in listed if c.state == "quiet"]
        causes = tuple(c for c in listed if c.state != "quiet")
        red = [c for c in causes if c.state == "red"]
        if red:
            top = red[0]
            many = (
                f"{len(red)} things have nobody on them or need you"
                if len(red) > 1
                else "One thing has nobody on it or needs you"
            )
            who = "It needs you." if top.needs_operator else "Nobody is on it."
            return f"{many}: {top.title.rstrip('.')}.", f"{top.detail} {who}".strip()
        if causes:
            top = causes[0]
            return (
                f"Work is moving, with {len(causes)} known holdups and a lane on each: {top.title.rstrip('.')}.",
                f"{top.detail} Lane {', '.join(top.lanes[:2])} is on it. Nothing is needed from you.".strip(),
            )
        subline = "Every read is fresh and every lab source is working."
        if quiet:
            plural = "s" if len(quiet) > 1 else ""
            subline += (
                f" {len(quiet)} earlier stoppage{plural} had no row since in the rollup;"
                " they are listed below with their age."
            )
        return "Nothing is holding things up.", subline

    @staticmethod
    def _rebuild(
        r: ModelFloorSimpleInput, rows: list[ModelFloorPieceRow]
    ) -> ModelRebuild:
        ov = r.overlay
        go = r.go_given_through
        if go is None:
            current = ov.rebuild.current_piece
        else:
            # The piece the rebuild is on: the first one the go covers that is not working yet, else the one after the go.
            current = next(
                (
                    x.piece
                    for x in rows
                    if x.piece <= go
                    and x.piece not in ov.rebuild.step0_pieces
                    and x.status != "working"
                ),
                min(go + 1, max(x.piece for x in rows)),
            )
        row = next(x for x in rows if x.piece == current)
        following = next((x for x in rows if x.piece == current + 1), None)
        if go is None:
            gate = "The go could not be read from the ledger. "
        elif current > go:
            gate = "Waiting on your go. "
        else:
            gate = ""
        status = (
            "not started"
            if row.status == "not_started"
            else row.status.replace("_", " ")
        )
        headline = f"Piece {current}, {row.name.lower()}: {status}."
        next_text = gate + (
            f"Next is {following.name.lower()}."
            if following
            else "This is the last piece."
        )
        return ModelRebuild(
            current_piece=current,
            headline=headline,
            next_text=next_text,
            go_given_through=go,
        )
