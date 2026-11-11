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

from collections.abc import Callable, Mapping
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
    ModelFloorReadiness,
    ModelFloorShadowLine,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelJoinDef,
    ModelJoinReceipt,
    ModelLabSourceVerdict,
    ModelOwnerInputs,
    ModelRebuild,
    ModelRedRule,
    ModelShadowReading,
)
from omnimarket.models.floor_17a.model_worklist import (
    WORKLIST_KINDS,
    ModelWorklistClass,
    ModelWorklistItem,
    ModelWorklistItemIn,
    WorklistKind,
)

# The one row that names the open pull requests citing no ticket; the overlay's `causes.titles` may reword it.
UNTICKETED_PRS_ID = "prs:no-ticket"


def _join_order(join_id: str) -> int:
    """Joins in number order, ``J-cycle`` last."""
    return int(join_id[1:]) if join_id[1:].isdigit() else 99


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


def _age_span(seconds: int) -> str:
    """A span in whole minutes from two minutes up, so an age and its bound read in one unit."""
    return f"{seconds // 60} min" if seconds >= 120 else f"{seconds} s"


def _cause_draw(cause: ModelFloorCause | None) -> str:
    """How a row that stands on a cause draws: red with no cause or a red one, grey when the cause is quiet."""
    if cause is None or cause.state == "red":
        return "red"
    return "grey" if cause.state == "quiet" else "amber"


def owner_inputs_reading(r: ModelFloorSimpleInput) -> ModelOwnerInputs | None:
    """Judge the two inputs of "who is on it" by their age (OMN-23388); ``None`` when the overlay declares no judgment.

    The owner map is read through the lab source the overlay names (the overlay store): it is unknown when that source
    was not reported, was not read, or its read is older than the source's own declared bound. The lane list is the
    floor record's lanes: unknown when the record was not read or is older than the overlay's bound. Either makes a row
    with no lane on it read owner unknown, with the age, rather than unowned.
    """
    spec = r.overlay.causes.owner_inputs
    if spec is None:
        return None
    reasons: list[str] = []
    verdict = next(
        (v for v in (r.lab.verdicts if r.lab else ()) if v.id == spec.owner_map_source),
        None,
    )
    map_age = verdict.age_s if verdict is not None else None
    map_max = verdict.max_age_s if verdict is not None else None
    if verdict is None:
        reasons.append(
            f"owner map unreadable: source {spec.owner_map_source} not reported"
        )
    elif map_age is None:
        reasons.append(
            f"owner map unreadable: {verdict.reason or 'the source was not read'}"
        )
    elif map_max is not None and map_age > map_max:
        reasons.append(
            f"owner map read {_age_span(map_age)} ago, the limit is {_age_span(map_max)}"
        )
    lanes_age = (
        max(int((r.now - r.record_read_at).total_seconds()), 0)
        if r.record_read_at is not None
        else None
    )
    if lanes_age is None:
        reasons.append("lane list unreadable: the floor record was not read")
    elif lanes_age > spec.lanes_max_age_s:
        reasons.append(
            f"lane list read {_age_span(lanes_age)} ago, the limit is {_age_span(spec.lanes_max_age_s)}"
        )
    return ModelOwnerInputs.model_validate(
        {
            "unknown": bool(reasons),
            "reason": "; ".join(reasons),
            "owner_map_age_s": map_age,
            "owner_map_max_age_s": map_max,
            "lanes_age_s": lanes_age,
            "lanes_max_age_s": spec.lanes_max_age_s,
        }
    )


def live_lanes(
    lanes: tuple[ModelFloorLane, ...],
    owners: tuple[str, ...],
    prs: tuple[str, ...] = (),
    cause_id: str = "",
) -> tuple[str, ...]:
    """The lanes that are really on a cause: not stalled, and not dead or finished by the process reading.

    A lane is on a cause when its CLAIM or a STATUS row says ``owns=floor:<cause id>``, or it works on one of the
    cause's pull requests, or its ticket, or the ``parent_ticket=`` its CLAIM names, is one the overlay or a lab source
    declares as owning the cause (a lane on a child ticket is on its parent's work). A ticket that merely appears among
    the places a problem was seen is not ownership: many unrelated lanes share an umbrella ticket, so matching on it names lanes that are not on the cause.
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
                or (lane.parent_ticket and lane.parent_ticket in wanted_owners)
                or (lane.pr and lane.pr in wanted_prs)
                or (cause_id and cause_id in lane.owns)
            )
        )
    )


def _resolved_after_last_seen(cause: ModelFloorCauseIn) -> bool:
    """True when a STATUS row reported the cause resolved at or after its newest row: it ended, on evidence."""
    return (
        cause.resolved_at is not None
        and cause.last_seen is not None
        and cause.resolved_at >= cause.last_seen
    )


def judge_worklist_item(
    item: ModelWorklistItemIn,
    lanes: tuple[ModelFloorLane, ...],
    owner_unknown: bool = False,
) -> ModelWorklistItem:
    """An item with its owner state, judged the way every other row judges work under way (OMN-23184).

    A live lane on the item (its pull requests, or its ticket, or a lane whose CLAIM says ``owns=floor:worklist:<key>``)
    owns it; failing that the landing lease on the pull request, then the lease on the cause that holds it; else nobody,
    or unknown when the lane list or the owner map could not be trusted (``owner_unknown``, OMN-23388).
    """
    on = live_lanes(
        lanes,
        (item.ticket,) if item.ticket else (),
        item.prs,
        f"worklist:{item.key}",
    )
    if on:
        state, owner = "live_lane", ", ".join(on)
    elif item.landing_lease:
        state, owner = "landing_lease", item.landing_lease
    elif item.cause_lease:
        state, owner = "cause_lease", item.cause_lease
    else:
        state, owner = ("unknown" if owner_unknown else "unowned"), ""
    return ModelWorklistItem.model_validate(
        {
            "kind": item.kind,
            "key": item.key,
            "title": item.title,
            "detail": item.detail,
            "red_checks": item.red_checks,
            "prs": item.prs,
            "ticket": item.ticket,
            "age_s": item.age_s,
            "conflicting": item.conflicting,
            "owner_state": state,
            "owner": owner,
        }
    )


def build_worklist(
    items: tuple[ModelWorklistItemIn, ...],
    lanes: tuple[ModelFloorLane, ...],
    titles: Mapping[WorklistKind, str],
    page_size: int,
    owner_unknown: bool = False,
) -> tuple[ModelWorklistClass, ...]:
    """One row per class, every member in pages: unowned first, then unknown, then oldest first, then by key."""
    out: list[ModelWorklistClass] = []
    for kind in WORKLIST_KINDS:
        judged = [
            judge_worklist_item(i, lanes, owner_unknown)
            for i in items
            if i.kind == kind
        ]
        judged.sort(
            key=lambda i: (
                i.owner_state not in {"unowned", "unknown"},
                i.owner_state != "unowned",
                -(i.age_s or 0),
                i.key,
            )
        )
        unowned = sum(1 for i in judged if i.owner_state == "unowned")
        unknown = sum(1 for i in judged if i.owner_state == "unknown")
        out.append(
            ModelWorklistClass.model_validate(
                {
                    "kind": kind,
                    "title": titles[kind],
                    "total": len(judged),
                    "unowned": unowned,
                    "unknown": unknown,
                    "owned": len(judged) - unowned - unknown,
                    "page_size": page_size,
                    "pages": tuple(
                        tuple(judged[n : n + page_size])
                        for n in range(0, len(judged), page_size)
                    ),
                }
            )
        )
    return tuple(out)


def worklist_causes(
    worklist: tuple[ModelWorklistClass, ...], owner_note: str = ""
) -> tuple[ModelFloorCause, ...]:
    """A row per class that holds an item: red while any member has nobody on it (or its owner is unknown), amber when every member has an owner."""
    out: list[ModelFloorCause] = []
    for cls in worklist:
        if cls.total == 0:
            continue
        members = [i for page in cls.pages for i in page]
        unowned = [i for i in members if i.owner_state == "unowned"]
        unknown = [i for i in members if i.owner_state == "unknown"]
        owners = sorted({i.owner for i in members if i.owner})
        named = unowned or unknown or members
        head = ", ".join(i.key for i in named[:4])
        more = len(named) - 4
        detail = (
            f"{cls.unowned} of {cls.total} have nobody on them"
            + (f"; owner unknown for {cls.unknown}" if cls.unknown else "")
            + f": {head}"
        )
        out.append(
            ModelFloorCause.model_validate(
                {
                    "id": f"worklist:{cls.kind}",
                    "title": cls.title,
                    "detail": detail + (f"; and {more} more." if more > 0 else "."),
                    "size": cls.total,
                    "size_unit": "work items",
                    "state": "red" if unowned or unknown else "amber",
                    "work_under_way": not (unowned or unknown),
                    "owner_state": "unowned"
                    if unowned
                    else "unknown"
                    if unknown
                    else "owned",
                    "owner_note": owner_note if unknown else "",
                    "needs_operator": False,
                    "lanes": tuple(owners),
                    "tickets": tuple(
                        dict.fromkeys(i.ticket for i in members if i.ticket)
                    ),
                    "owners": (),
                    "prs": tuple(dict.fromkeys(p for i in members for p in i.prs)),
                }
            )
        )
    return tuple(out)


def unowned_red_causes(document: ModelFloorSimple) -> tuple[str, ...]:
    """The id of every red cause (listed or not) that declares no owner, in the document's order.

    An owner is declared by the overlay's ``causes.owners`` map or by a lab source's tickets; a cause that stands for
    its members is judged through them. A red cause with no declared owner reads ``owners: []`` on the board, so nobody
    is shown on it however many lanes work, and no check fails: this reading names it.
    """
    return tuple(
        c.id
        for c in document.causes
        if c.state == "red"
        and not c.members
        and not c.owners
        and c.owner_state != "unknown"
    )


def owner_unknown_red_causes(document: ModelFloorSimple) -> tuple[str, ...]:
    """The id of every red cause no live lane is on while the owner map or lane list was stale or unread (OMN-23388).

    The board cannot say nobody is on these, so they are named here and left out of ``unowned_red_causes`` and
    ``uncarried_red_causes``: a stale map would otherwise read as an absent owner.
    """
    return tuple(
        c.id
        for c in document.causes
        if c.state == "red" and not c.members and c.owner_state == "unknown"
    )


def uncarried_red_causes(
    document: ModelFloorSimple, lanes: tuple[ModelFloorLane, ...]
) -> tuple[str, ...]:
    """The id of every red cause that no live lane is on, in the document's order (OMN-23243).

    A cause lists owner tickets, but a ticket in that list is a name and not a carrier: the cause is carried only when a
    live lane is on it (``live_lanes``: a lane on one of its owner tickets, its pull requests, or its own ``owns=``).
    A cause that stands for its members is judged through them. This is separate from ``unowned_red_causes``, which
    reads whether an owner is declared at all.
    """
    return tuple(
        c.id
        for c in document.causes
        if c.state == "red"
        and not c.members
        and c.owner_state != "unknown"
        and not live_lanes(lanes, c.owners, c.prs, c.id)
    )


def check_red_rule(
    document: ModelFloorSimple, lanes: tuple[ModelFloorLane, ...]
) -> ModelRedRule:
    """The reading of the colour rule, recomputed from the document and the lanes only."""
    violations: list[str] = []
    with_work = 0
    red_rows = sum(1 for c in document.causes if c.listed and c.state == "red")
    known = {c.id: c for c in document.causes}
    by_kind: dict[str, ModelWorklistClass] = {
        c.kind: c for c in (document.worklist or ())
    }
    for cause in document.causes:
        if cause.id.startswith("worklist:"):
            cls = by_kind.get(cause.id.removeprefix("worklist:"))
            if cls is None:
                violations.append(f"{cause.id} draws with no work list class behind it")
                continue
            expected = "red" if cls.unowned or cls.unknown else "amber"
            if cause.state != expected:
                violations.append(
                    f"{cause.id} draws {cause.state} but {cls.unowned} of its {cls.total} items have nobody on them"
                )
            for page in cls.pages:
                for item in page:
                    on_it = live_lanes(
                        lanes,
                        (item.ticket,) if item.ticket else (),
                        item.prs,
                        f"worklist:{item.key}",
                    )
                    if item.owner_state in {"unowned", "unknown"} and on_it:
                        violations.append(
                            f"{item.key} reads {item.owner_state} while {', '.join(on_it)} is on it"
                        )
                    if item.owner_state == "live_lane" and not on_it:
                        violations.append(
                            f"{item.key} reads a live lane with none on it"
                        )
            continue
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
    # Readiness (OMN-23081), read again from the finished document: a piece reads ready only when its own reading works and
    # every counted join at either end passes, and a counted join that does not pass is a listed row with an owner ticket.
    all_joins = {j.id: j for p in document.pieces for j in p.joins}
    for piece in document.pieces:
        if not piece.ready:
            continue
        if piece.status != "working":
            violations.append(
                f"piece {piece.piece} reads ready while its own reading is {piece.status.replace('_', ' ')}"
            )
        violations.extend(
            f"piece {piece.piece} reads ready while join {j.id} reads {j.state.replace('_', ' ')}"
            for j in all_joins.values()
            if j.counted and not j.passes and piece.piece in (j.from_piece, j.to_piece)
        )
        violations.extend(
            f"piece {piece.piece} reads ready while join {j.id} saw no real work"
            for j in all_joins.values()
            if j.counted
            and not j.real_passes
            and piece.piece in (j.from_piece, j.to_piece)
        )
    for join in all_joins.values():
        if not join.counted or join.passes or join.state == "unread":
            continue
        row = known.get(f"join:{join.id}")
        if row is None or not row.listed:
            violations.append(
                f"join {join.id} reads {join.state.replace('_', ' ')} and has no row"
            )
        elif not row.owners:
            violations.append(f"join {join.id} has a row and no owner ticket")
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
    uncarried = uncarried_red_causes(document, lanes)
    owner_unknown = owner_unknown_red_causes(document)
    state = (
        "NOT WORKING"
        if violations or uncarried
        else "UNKNOWN"
        if owner_unknown
        else "WORKING"
    )
    return ModelRedRule.model_validate(
        {
            "state": state,
            "red_rows": red_rows,
            "red_with_work_under_way": with_work,
            "violations": tuple(violations),
            "unowned_red": unowned_red_causes(document),
            "uncarried_red": uncarried,
            "owner_unknown_red": owner_unknown,
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
        inputs = owner_inputs_reading(request)
        causes = self._causes(candidates, request)
        worklist = self._worklist(request, inputs)
        if worklist is not None:
            causes = tuple(
                sorted(
                    (
                        *causes,
                        *worklist_causes(
                            worklist, inputs.reason if inputs is not None else ""
                        ),
                    ),
                    key=lambda c: (c.state == "quiet", -c.size, c.id),
                )
            )
        by_id = {c.id: c for c in causes}
        pieces = self._integrate(
            request, tuple(self._row_draw(r, by_id) for r in rows), by_id
        )
        readiness = self._readiness(request, pieces)
        lines = tuple(self._lab_line(v, by_id) for v in lab)
        level = self._level(causes)
        sentence, subline = self._words(causes)
        subline = " ".join(
            part
            for part in (
                subline,
                readiness.text,
                readiness.probe_health_text,
                readiness.real_work_notice,
            )
            if part
        )
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
                "rebuild": self._rebuild(request, list(pieces)),
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
                "worklist": worklist,
                "readiness": readiness,
                "owner_inputs": inputs,
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

    @staticmethod
    def _worklist(
        r: ModelFloorSimpleInput, inputs: ModelOwnerInputs | None = None
    ) -> tuple[ModelWorklistClass, ...] | None:
        """The work list's classes, or ``None`` when the overlay declares none or the sources did not read it."""
        wl = r.overlay.worklist
        if wl is None or r.worklist is None:
            return None
        return build_worklist(
            r.worklist,
            r.lanes,
            wl.titles,
            wl.page_size,
            owner_unknown=inputs is not None and inputs.unknown,
        )

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
        criteria = {c.piece: c for c in ov.rebuild.start_criteria}
        working = self._working_pieces(r, lab) if criteria else frozenset[int]()
        for definition in sorted(ov.pieces, key=lambda p: p.piece):
            n = definition.piece
            rc = receipts.get(n)
            go = r.go_given_through
            # A piece starts on a go, as step 0, or when its own start criterion reads true (RULING 2026-11-07T12:20:46Z).
            admitted = (
                (go is not None and n <= go)
                or n in r.go_pieces
                or n in ov.rebuild.step0_pieces
                or (n in criteria and set(criteria[n].after_working) <= working)
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
                            self._join_row(r, j, joins.get(j.id))
                            for j in ov.joins
                            if j.from_piece == n
                        ),
                        "shadows": tuple(
                            self._shadow_line(s, r.now)
                            for s in r.shadows
                            if s.piece == n
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
        """How many chains of ``piece`` ended as ``kind`` inside the window.

        ``None`` (unread, never zero) when the receipts were not read, when they reach back less far than the window, or
        when the ledger holding the piece's events could not be read and no local chain of the piece stands in for it.
        """
        if r.receipts is None:
            return None
        if r.receipts_covered_s is not None and window_s > r.receipts_covered_s:
            return None
        if r.sources_read.get("ledger") is False and not any(
            c.piece == piece for c in r.receipts.chains
        ):
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

    @classmethod
    def _working_pieces(
        cls, r: ModelFloorSimpleInput, lab: tuple[ModelLabSourceVerdict, ...]
    ) -> frozenset[int]:
        """The pieces whose reading reads WORKING now, as the strip would draw them (lab sources folded in)."""
        out: set[int] = set()
        for definition in r.overlay.pieces:
            n = definition.piece
            state, _age, _max, why = cls._reading(
                r, n, definition.reading_max_age_s, lab
            )
            state, _why, _cause = cls._with_lab_sources(r, n, lab, state, why)
            if state == "WORKING":
                out.add(n)
        return frozenset(out)

    @staticmethod
    def _shadow_line(shadow: ModelShadowReading, now: datetime) -> ModelFloorShadowLine:
        """A shadow row as the piece's line: its cells as the rule labels them, and its age against the age it declared."""
        age = max(0, int((now - shadow.published_at).total_seconds()))
        return ModelFloorShadowLine(
            label=shadow.label,
            text=(
                f"not read: {shadow.failed_outcome}"
                if shadow.failed_outcome is not None
                else ", ".join(f"{label} {value}" for label, value in shadow.values)
            ),
            age_s=age,
            max_age_s=shadow.max_age_s,
            stale=age > shadow.max_age_s,
        )

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
        r: ModelFloorSimpleInput,
        definition: ModelJoinDef,
        receipt: ModelJoinReceipt | None,
    ) -> ModelFloorJoinRow:
        """One join as drawn: the receipt's state, whether it counts toward READY and whether it passes (OMN-23081)."""
        now = r.now
        rd = r.overlay.readiness
        names = {p.piece: p.name for p in r.overlay.pieces}
        golden = error = last_run = None
        state = "unread"
        if receipt is not None:
            state = receipt.state
            if receipt.golden_at:
                golden = max(int((now - receipt.golden_at).total_seconds()), 0)
            if receipt.error_at:
                error = max(int((now - receipt.error_at).total_seconds()), 0)
            ran = [x for x in (golden, error) if x is not None]
            last_run = min(ran) if ran else None
        scope = set(rd.in_scope_pieces)
        # Real work (OMN-23862): the golden chain that came from real work, inside the window the receipts cover (the
        # emission window; the overlay's event window when the receipts say none).
        window_s = r.receipts_covered_s or r.overlay.rebuild.event_window_s
        real_golden = (
            max(int((now - receipt.real_golden_at).total_seconds()), 0)
            if receipt is not None and receipt.real_golden_at
            else None
        )
        return ModelFloorJoinRow.model_validate(
            {
                "id": definition.id,
                "label": definition.label,
                "to_piece": definition.to_piece,
                "state": state,
                "golden_age_s": golden,
                "error_age_s": error,
                "from_piece": definition.from_piece,
                "counted": definition.from_piece in scope
                and definition.to_piece in scope,
                "passes": state in rd.passing_join_states,
                "definition": (
                    f"{definition.label}: piece {definition.from_piece}, "
                    f"{names[definition.from_piece].lower()}, to piece {definition.to_piece}, "
                    f"{names[definition.to_piece].lower()}"
                ),
                "last_run_age_s": last_run,
                "real_golden_age_s": real_golden,
                # Real work is not configured (the overlay names no real_origins): the old rule applies and a join that
                # passes counts as it did before OMN-23862.
                "real_passes": state in rd.passing_join_states
                and (
                    not rd.real_work_configured
                    or (real_golden is not None and real_golden <= window_s)
                ),
            }
        )

    @staticmethod
    def _join_failure(
        r: ModelFloorSimpleInput,
        join: ModelFloorJoinRow,
        rows: list[ModelFloorPieceRow],
    ) -> tuple[str, str]:
        """Which side of a join failed, and why: the chain finding that names it, else the piece that does not work, else neither.

        ``from`` is the upstream piece of the join and ``to`` the downstream one. A chain finding blames the side the
        overlay's ``finding_sides`` gives its code. With no finding the join never ran or ran halfway: the side is the
        piece whose own reading is not WORKING, and ``neither`` when both read WORKING and no cycle ran the join.
        """
        rd = r.overlay.readiness
        subject = f"{join.id} piece "
        findings = [
            f
            for f in (r.receipts.findings if r.receipts is not None else ())
            if f.subject.startswith(subject)
        ]
        if findings:
            side = rd.finding_sides.get(findings[0].code, "to")
            # One line per distinct finding, with how many chains it was found in.
            counts: dict[str, int] = {}
            for f in findings:
                line = f"{f.code}: {f.detail}"
                counts[line] = counts.get(line, 0) + 1
            return side, "; ".join(
                f"{line} (in {n} chains)" if n > 1 else line
                for line, n in list(counts.items())[:3]
            )
        if r.receipts is None:
            return "neither", "the join receipts could not be read"
        by_piece = {x.piece: x for x in rows}
        what = {
            "not_run": "no golden and no error chain of this join has run",
            "golden_only": "the golden chain ran and no error chain has",
            "error_only": "the error chain ran and no golden chain has",
        }.get(join.state, f"the join reads {join.state}")
        for side, piece in (("from", join.from_piece), ("to", join.to_piece)):
            row = by_piece[piece]
            if row.status != "working":
                return (
                    side,
                    f"{what}; piece {piece}, {row.name.lower()}, is {row.status.replace('_', ' ')}",
                )
        return "neither", f"{what}; both pieces read WORKING and no cycle has run both"

    # --- readiness (OMN-23081) ----------------------------------------------------------------------------------------

    def _integrate(
        self,
        r: ModelFloorSimpleInput,
        pieces: tuple[ModelFloorPieceRow, ...],
        by_id: dict[str, ModelFloorCause],
    ) -> tuple[ModelFloorPieceRow, ...]:
        """Add what each join owes the board and say which pieces are READY.

        A piece is READY only when it is in scope, its own reading is WORKING, at least ``min_joins`` joins count for it and
        every counted join at either of its ends passes. A piece that works and is not ready draws amber and says
        ``working, not integrated``. Each counted join that does not pass gets its owner tickets, the lanes on it, the side
        that failed and the reason from its red row.
        """
        rd = r.overlay.readiness
        scope = set(rd.in_scope_pieces)
        rows = list(pieces)
        status = {row.piece: row.status for row in rows}
        window_h = (r.receipts_covered_s or r.overlay.rebuild.event_window_s) // 3600
        joins: dict[str, ModelFloorJoinRow] = {}
        for row in rows:
            for join in row.joins:
                joins[join.id] = self._join_owed(r, join, rows, by_id)
        out: list[ModelFloorPieceRow] = []
        for row in rows:
            counted = sorted(
                (
                    j
                    for j in joins.values()
                    if j.counted and row.piece in (j.from_piece, j.to_piece)
                ),
                key=lambda j: _join_order(j.id),
            )
            blocking = tuple(j.id for j in counted if not j.passes)
            # A join that passes on probe chains alone is health, never integration (OMN-23862).
            probe_only = tuple(j.id for j in counted if j.passes and not j.real_passes)
            neighbours = tuple(
                sorted(
                    {
                        other
                        for j in counted
                        for other in (j.from_piece, j.to_piece)
                        if other != row.piece
                        and status.get(other) in rd.blocking_neighbour_statuses
                    }
                )
            )
            in_scope = row.piece in scope
            working = row.status == "working"
            ready = (
                in_scope
                and working
                and len(counted) >= rd.min_joins
                and not blocking
                and not probe_only
                and not neighbours
            )
            if ready:
                integration = "ready"
            elif working:
                integration = "working_not_integrated"
            elif row.status == "not_started":
                integration = "not_started"
            else:
                integration = "not_working"
            update: dict[str, object] = {
                "joins": tuple(joins[j.id] for j in row.joins),
                "ready": ready,
                "in_scope": in_scope,
                "integration": integration,
                "blocking_joins": blocking,
                "counted_joins": len(counted),
                "probe_only_joins": probe_only,
                "blocking_neighbours": neighbours,
            }
            if integration == "working_not_integrated":
                reasons: list[str] = []
                if blocking:
                    reasons.append(
                        ", ".join(
                            f"{j} {joins[j].state.replace('_', ' ')}" for j in blocking
                        )
                    )
                if probe_only:
                    reasons.append(
                        "green on probes only: no real work crossed "
                        + ("this join" if len(probe_only) == 1 else "these joins")
                        + f" in {window_h} hours ({', '.join(probe_only)})"
                    )
                if neighbours:
                    reasons.append(
                        "; ".join(
                            f"neighbour piece {n} reads "
                            f"{status[n].replace('_', ' ').upper()}"
                            for n in neighbours
                        )
                    )
                if not reasons and not in_scope:
                    reasons.append("outside the pieces in scope")
                elif not reasons:
                    reasons.append(
                        f"fewer than {rd.min_joins} join to a piece in scope"
                    )
                why = "; ".join(reasons)
                update["draw"] = "amber"
                update["text"] = f"working, not integrated ({why}) · {row.text}"
                update["cause_id"] = f"join:{blocking[0]}" if blocking else None
            elif ready:
                update["text"] = (
                    "READY: reading WORKING and "
                    + ", ".join(sorted(j.id for j in counted))
                    + f" pass on real work · {row.text}"
                )
            out.append(row.model_copy(update=update))
        return tuple(out)

    def _join_owed(
        self,
        r: ModelFloorSimpleInput,
        join: ModelFloorJoinRow,
        rows: list[ModelFloorPieceRow],
        by_id: dict[str, ModelFloorCause],
    ) -> ModelFloorJoinRow:
        cause = by_id.get(f"join:{join.id}")
        if cause is None:
            return join
        side, reason = self._join_failure(r, join, rows)
        return join.model_copy(
            update={
                "cause_id": cause.id,
                "failed_side": side,
                "failed_reason": reason,
                "owners": cause.owners,
                "lanes": cause.lanes,
            }
        )

    @staticmethod
    def _readiness(
        r: ModelFloorSimpleInput, pieces: tuple[ModelFloorPieceRow, ...]
    ) -> ModelFloorReadiness:
        """The count the board reports: ready pieces, not working ones."""
        scope = tuple(sorted(r.overlay.readiness.in_scope_pieces))
        in_scope = [p for p in pieces if p.in_scope]
        ready = tuple(p.piece for p in in_scope if p.ready)
        wni = tuple(
            p.piece for p in in_scope if p.integration == "working_not_integrated"
        )
        down = tuple(
            p.piece for p in in_scope if p.integration in ("not_working", "not_started")
        )
        broken = tuple(
            sorted(
                {j.id for p in pieces for j in p.joins if j.counted and not j.passes},
                key=_join_order,
            )
        )
        by_piece = {p.piece: p for p in pieces}
        text = (
            f"{len(ready)} of {len(scope)} pieces in scope are ready"
            + (f" (pieces {', '.join(map(str, ready))})" if ready else "")
            + f"; {len(wni)} work but are not integrated"
            + (f" (pieces {', '.join(map(str, wni))})" if wni else "")
            # Every in-scope piece is named (OMN-23862): the ones that do not work, each with its state.
            + f"; {len(down)} do not work"
            + (
                " ("
                + ", ".join(
                    f"piece {n} {by_piece[n].status.replace('_', ' ')}" for n in down
                )
                + ")"
                if down
                else ""
            )
            + f"; {len(broken)} joins do not pass"
            + (f" ({', '.join(broken)})" if broken else "")
            + "."
        )
        # Probe health, apart from integration (OMN-23862): what the probe chains say, which never counts toward READY.
        counted_joins = {j.id: j for p in pieces for j in p.joins if j.counted}
        health = tuple(
            sorted((i for i, j in counted_joins.items() if j.passes), key=_join_order)
        )
        probe_only = tuple(i for i in health if not counted_joins[i].real_passes)
        configured = r.overlay.readiness.real_work_configured
        probe_text = (
            f"Probe health: {len(health)} of {len(counted_joins)} counted joins pass on probe chains"
            f"; {len(probe_only)} of them saw no real work in the window"
            + (f" ({', '.join(probe_only)})" if probe_only else "")
            + ". Probe chains never count toward ready."
            if configured
            else ""
        )
        # One typed line when the overlay carries no real_origins: the old rule applies (OMN-23862), never silence.
        notice = (
            ""
            if configured
            else "Real-work rule not configured (the readiness overlay names no real_origins): the old rule applies and a join "
            "counts on its probe chains."
        )
        return ModelFloorReadiness(
            in_scope=scope,
            ready=ready,
            working_not_integrated=wni,
            not_working=down,
            broken_joins=broken,
            text=text,
            probe_health_joins=health,
            probe_only_joins=probe_only,
            probe_health_text=probe_text,
            real_work_rule="configured" if configured else "not_configured",
            real_work_notice=notice,
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
            resolved_at: datetime | None = None,
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
                        "resolved_at": resolved_at,
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
        if r.unticketed_prs:
            add(
                UNTICKETED_PRS_ID,
                "Pull requests cite no ticket",
                f"{len(r.unticketed_prs)} open pull requests name no ticket in the title or a Ticket line of the body.",
                len(r.unticketed_prs),
                "pull requests",
                (),
                prs=r.unticketed_prs,
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
                resolved_at=item.resolved_at,
            )
        self._delegation_idle(r, add)
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
        for row in rows:
            for join in row.joins:
                if not join.counted or join.passes or r.receipts is None:
                    continue
                side, reason = self._join_failure(r, join, rows)
                names = {p.piece: p.name.lower() for p in r.overlay.pieces}
                side_piece = {
                    "from": join.from_piece,
                    "to": join.to_piece,
                }.get(side)
                where = (
                    f"the {side} side, piece {side_piece}, {names[side_piece]}"
                    if side_piece
                    else "neither side alone"
                )
                state_words = {
                    "not_run": "has never run",
                    "golden_only": "has only half run",
                    "error_only": "has only half run",
                    "broken": "is broken",
                }.get(join.state, f"reads {join.state}")
                ran = (
                    _ago(join.last_run_age_s)
                    if join.last_run_age_s is not None
                    else "never run"
                )
                add(
                    f"join:{join.id}",
                    f"Join {join.id} {state_words}: {join.label}",
                    f"{join.definition}. Failed: {where}. {reason}. Last run: {ran}.",
                    1,
                    "joins",
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
    def _delegation_idle(r: ModelFloorSimpleInput, add: Callable[..., None]) -> None:
        """The delegation:idle row (OMN-23313): operator-owned delegation tickets that can start, and who is on them.

        The sources hand over only the tickets the operator owns, under a delegation epic, whose start criterion reads
        true. A ticket is idle when no live lane is on it (``live_lanes``: its own ticket or a ``parent_ticket=``). The row
        owns the idle tickets, so it draws red while any has no lane, and owns every ticket once each has one, so it draws
        amber then. The overlay's ``causes.owners`` entry for the row adds the tickets whose lanes work on the row itself.
        """
        spec = r.overlay.causes.delegation_idle
        if spec is None or not r.delegation_tickets:
            return
        tickets = tuple(dict.fromkeys(t.ticket for t in r.delegation_tickets))
        idle = tuple(t for t in tickets if not live_lanes(r.lanes, (t,)))
        shown = ", ".join((idle or tickets)[:6])
        more = len(idle or tickets) - 6
        tail = f"; and {more} more" if more > 0 else ""
        detail = (
            f"{len(idle)} of {len(tickets)} operator-owned delegation tickets can start and have no lane on them: "
            f"{shown}{tail}."
            if idle
            else f"All {len(tickets)} operator-owned delegation tickets that can start have a lane: {shown}{tail}."
        )
        add(
            "delegation:idle",
            spec.title,
            detail,
            len(idle) or len(tickets),
            "delegation tickets",
            tickets,
            owners=idle or tickets,
        )

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
            if item.resolved_at is not None and _resolved_after_last_seen(item):
                resolved_age = max(int((r.now - item.resolved_at).total_seconds()), 0)
                seen += f", resolved {_ago(resolved_age)} by a STATUS row, no row since"
            elif ov.still_occurring_within_s is not None:
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
        inputs = owner_inputs_reading(r)
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
            # A STATUS row that reported the stoppage resolved after its newest row is the evidence it ended, whatever the window.
            if _resolved_after_last_seen(c):
                occurring = False
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
                        "owner_state": "owned"
                        if lanes
                        else "unknown"
                        if inputs is not None and inputs.unknown
                        else "unowned",
                        "owner_note": inputs.reason
                        if not lanes and inputs is not None and inputs.unknown
                        else "",
                        "needs_operator": c.needs_operator,
                        "lanes": lanes,
                        "tickets": c.tickets,
                        "owners": c.owners,
                        "prs": c.prs,
                        "last_seen": c.last_seen,
                        "last_seen_age_s": age,
                        "still_occurring": occurring,
                        "resolved_at": c.resolved_at,
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
                        "owner_state": "unknown"
                        if any(m.owner_state == "unknown" for m in members)
                        else "unowned"
                        if red
                        else "owned",
                        "owner_note": next(
                            (m.owner_note for m in members if m.owner_note), ""
                        ),
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
    def _finished(row: ModelFloorPieceRow) -> bool:
        """A piece the rebuild is done with: ready when it is in scope, working when it is not."""
        return row.ready if row.in_scope else row.status == "working"

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
                    if (x.piece <= go or x.piece in r.go_pieces)
                    and x.piece not in ov.rebuild.step0_pieces
                    and not HandlerFloorSimple._finished(x)
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
            "working, not integrated"
            if row.integration == "working_not_integrated"
            else "ready"
            if row.ready
            else "not started"
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
            go_pieces=r.go_pieces,
        )
