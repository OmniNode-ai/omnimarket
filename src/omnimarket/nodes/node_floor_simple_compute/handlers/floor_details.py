# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""What a number, a row or a piece card on the first page opens (OMN-23018).

Pure: ``build_details(request, document) -> {key: ModelFloorDetail}``. Every panel is built from the same readings as the
row it explains. The panel of a number lists the things the number counts, each line standing for ``count`` units, and
``counted`` is the sum of those units: a number whose lines do not add up is a defect the tests catch and the page shows.

Keys: ``number:merges``, ``number:open_prs``, ``number:slots``, ``number:red_rows``, ``alarms:open``, ``cause:<id>``,
``lab:<id>``, ``piece:<n>``.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta

from omnimarket.models.floor_17a import (
    Draw,
    ModelFloorAlarmLine,
    ModelFloorCause,
    ModelFloorDetail,
    ModelFloorDetailFact,
    ModelFloorDetailItem,
    ModelFloorDetailSection,
    ModelFloorJoinRow,
    ModelFloorLane,
    ModelFloorLinks,
    ModelFloorPieceRow,
    ModelFloorPrItem,
    ModelFloorSimple,
    ModelFloorSimpleInput,
    ModelLabSourceVerdict,
)

# What the join states mean in words, and how each draws.
JOIN_WORDS: dict[str, tuple[str, Draw]] = {
    "not_run": ("has not run", "grey"),
    "golden_only": ("has a golden chain and no error chain yet", "amber"),
    "error_only": ("has an error chain and no golden chain yet", "amber"),
    "passed": (
        "passed: golden and error chains both ended as the contract says",
        "green",
    ),
    "broken": ("is broken: a chain breaks the contract", "red"),
    "unread": ("was not read", "grey"),
}
CAUSE_TONE: dict[str, Draw] = {"red": "red", "amber": "amber", "quiet": "grey"}


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


class _Links:
    def __init__(self, links: ModelFloorLinks) -> None:
        self._links = links

    def ticket(self, ticket: str) -> str:
        return self._links.ticket_url.format(ticket=ticket)

    def pr(self, key: str) -> str:
        repo, _, number = key.rpartition("#")
        if not repo or not number.isdigit():
            return ""
        return self._links.pr_url.format(repo=repo, number=number)


def build_details(
    request: ModelFloorSimpleInput, document: ModelFloorSimple
) -> dict[str, ModelFloorDetail]:
    return _Builder(request, document).build()


class _Builder:
    def __init__(
        self, request: ModelFloorSimpleInput, document: ModelFloorSimple
    ) -> None:
        self.r = request
        self.doc = document
        self.link = _Links(request.overlay.links)
        self.lanes: Mapping[str, ModelFloorLane] = {
            lane.lane: lane for lane in request.lanes
        }
        self.causes: Mapping[str, ModelFloorCause] = {c.id: c for c in document.causes}
        self.verdicts: Mapping[str, ModelLabSourceVerdict] = {
            v.id: v for v in (request.lab.verdicts if request.lab else ())
        }
        self.open_prs: Mapping[str, ModelFloorPrItem] = {
            i.key: i for i in request.open_pr_items
        }
        self.numbers = {n.key: n for n in document.numbers}

    # --- shared line builders ------------------------------------------------------------------------------------------

    def _tickets(
        self, owners: tuple[str, ...], seen: tuple[str, ...]
    ) -> ModelFloorDetailSection:
        items = [
            ModelFloorDetailItem(
                title=t,
                href=self.link.ticket(t),
                tag="owner",
                sub="Declared as owning it: a lane on this ticket counts as work under way.",
            )
            for t in owners
        ]
        items += [
            ModelFloorDetailItem(
                title=t,
                href=self.link.ticket(t),
                tag="seen",
                sub="Named where the problem was seen; not proof anyone owns it.",
            )
            for t in seen
            if t not in owners
        ]
        return ModelFloorDetailSection(
            heading="Owner tickets",
            items=tuple(items),
            empty="No ticket is declared as owning it.",
        )

    def _lane_items(self, names: tuple[str, ...]) -> tuple[ModelFloorDetailItem, ...]:
        out: list[ModelFloorDetailItem] = []
        for name in names:
            lane = self.lanes.get(name)
            if lane is None:
                out.append(
                    ModelFloorDetailItem(title=name, sub="No lane row in the record.")
                )
                continue
            where = f"on {lane.host}" if lane.host else "on no host"
            parts = [
                lane.ticket,
                where,
                "gone quiet" if lane.stalled else lane.liveness,
            ]
            out.append(
                ModelFloorDetailItem(
                    title=lane.lane,
                    sub=" · ".join(p for p in parts if p),
                    href=self.link.ticket(lane.ticket) if lane.ticket else "",
                    at=lane.started_at,
                    tone="amber" if lane.stalled else None,
                )
            )
        return tuple(out)

    def _lanes(self, names: tuple[str, ...]) -> ModelFloorDetailSection:
        return ModelFloorDetailSection(
            heading="Lanes running on it",
            note="Ticket, host and start of each live lane on this holdup.",
            items=self._lane_items(names),
            empty="No live lane is on it.",
        )

    def _prs(self, keys: tuple[str, ...]) -> ModelFloorDetailSection:
        items = []
        for key in keys:
            known = self.open_prs.get(key)
            items.append(
                ModelFloorDetailItem(
                    title=f"{key} {known.title}" if known else key,
                    sub=known.reason if known else "",
                    href=self.link.pr(key),
                    at=known.at if known else None,
                )
            )
        return ModelFloorDetailSection(
            heading="Pull requests",
            items=tuple(items),
            empty="No pull request is tied to it on the floor.",
        )

    def _alarm_items(
        self, alarms: tuple[ModelFloorAlarmLine, ...]
    ) -> tuple[ModelFloorDetailItem, ...]:
        out: list[ModelFloorDetailItem] = []
        for a in alarms:
            out.append(
                ModelFloorDetailItem(
                    title=a.title,
                    sub=f"{a.delivery} via {a.channel}{', escalated' if a.escalated else ''}",
                    at=a.raised_at,
                    tag="raised",
                    tone="red" if a.delivery == "undelivered" else None,
                )
            )
            out += [
                ModelFloorDetailItem(
                    title=f"Sent to {s.channel} ({s.role})",
                    sub=f"{s.delivery}{': ' + s.detail if s.detail else ''}",
                    at=s.sent_at,
                    tag="send",
                    tone="red" if s.delivery == "failed" else None,
                )
                for s in a.sends
            ]
        return tuple(out)

    def _alarms(
        self, alarms: tuple[ModelFloorAlarmLine, ...]
    ) -> ModelFloorDetailSection:
        return ModelFloorDetailSection(
            heading="Alarm history",
            note="Open alarms and each send. A cleared alarm leaves no row, so only what is still open is here.",
            items=self._alarm_items(alarms),
            empty="No open alarm for it.",
        )

    def _alarms_for(self, source_ids: set[str]) -> tuple[ModelFloorAlarmLine, ...]:
        return tuple(a for a in self.doc.alarms if a.source_id in source_ids)

    def _fact_time(self, label: str, age_s: int | None) -> ModelFloorDetailFact:
        if age_s is None:
            return ModelFloorDetailFact(label=label, value="never read")
        return ModelFloorDetailFact(
            label=label, value="", at=self.r.now - timedelta(seconds=age_s)
        )

    # --- the numbers ---------------------------------------------------------------------------------------------------

    def build(self) -> dict[str, ModelFloorDetail]:
        out: dict[str, ModelFloorDetail] = {}
        for detail in (
            self._merges(),
            self._open_prs(),
            self._slots(),
            self._red_rows(),
            self._open_alarms(),
        ):
            out[detail.key] = detail
        for cause in self.doc.causes:
            out[f"cause:{cause.id}"] = self._cause(cause)
        for line in self.doc.lab_state:
            out[f"lab:{line.id}"] = self._lab(line.id)
        for piece in self.doc.pieces:
            out[f"piece:{piece.piece}"] = self._piece(piece)
        for join in self._joins().values():
            out[f"join:{join.id}"] = self._join(join)
        return out

    def _joins(self) -> dict[str, ModelFloorJoinRow]:
        return {j.id: j for p in self.doc.pieces for j in p.joins}

    def _unread(self, key: str, title: str, reason: str) -> ModelFloorDetail:
        return ModelFloorDetail(
            key=key,
            title=title,
            summary=f"Not read: {reason}",
            tone="red",
            sections=(
                ModelFloorDetailSection(
                    heading="Why there is no list",
                    empty="The number is drawn unread, so there are no lines behind it.",
                ),
            ),
        )

    def _merges(self) -> ModelFloorDetail:
        n = self.numbers["merges"]
        key, title = "number:merges", "Merges in the window"
        if not n.read or self.r.merges_in_window is None:
            return self._unread(key, title, n.label)
        window = self.r.overlay.numbers.merge_window_minutes
        items = tuple(
            ModelFloorDetailItem(
                title=f"{m.key} {m.title}".strip(),
                group=m.repo,
                href=self.link.pr(m.key),
                at=m.at,
                sub=" · ".join(
                    x
                    for x in (
                        ", ".join(m.tickets),
                        "counted because the landing controller admitted it"
                        if m.admitted_by_controller
                        else "",
                    )
                    if x
                ),
            )
            for m in sorted(
                self.r.merge_items, key=lambda m: (m.repo, m.at is None, m.at, m.number)
            )
        )
        outside = self.r.outside_goal
        facts = [
            ModelFloorDetailFact(label="Counted", value=n.value),
            ModelFloorDetailFact(label="Window", value=f"last {window} min"),
            ModelFloorDetailFact(
                label="Goal",
                value=f"{self.r.overlay.numbers.landing_goal_per_hour} an hour",
            ),
        ]
        if outside.read and outside.merges_in_window is not None:
            facts.append(
                ModelFloorDetailFact(
                    label="Outside the goal, not counted",
                    value=str(outside.merges_in_window),
                )
            )
        return ModelFloorDetail(
            key=key,
            title=title,
            summary=n.label,
            tone="green" if int(n.value) else "amber",
            facts=tuple(facts),
            sources=(n.basis,),
            total=int(n.value),
            counted=sum(i.count for i in items),
            sections=(
                ModelFloorDetailSection(
                    heading=f"Merged in the last {window} min, by repository",
                    items=items,
                    empty="Nothing in scope merged in the window.",
                ),
            ),
        )

    def _open_prs(self) -> ModelFloorDetail:
        n = self.numbers["open_prs"]
        key, title = "number:open_prs", "Open in-scope pull requests"
        if not n.read or self.r.open_prs is None:
            return self._unread(key, title, n.label)
        classes = self.r.overlay.causes.pr_classes
        items = tuple(
            ModelFloorDetailItem(
                title=f"{p.key} {p.title}".strip(),
                group=classes.get(p.cls, p.cls or "no class"),
                href=self.link.pr(p.key),
                at=p.at,
                tag="draft" if p.draft else "armed" if p.armed else "",
                sub=p.reason or "The watcher records no reason.",
            )
            for p in sorted(
                self.r.open_pr_items, key=lambda p: (p.cls, p.repo, p.number)
            )
        )
        return ModelFloorDetail(
            key=key,
            title=title,
            summary=n.label,
            tone="amber" if int(n.value) else "green",
            facts=(
                ModelFloorDetailFact(label="Counted", value=n.value),
                ModelFloorDetailFact(
                    label="Left out",
                    value=", ".join(self.r.overlay.numbers.excluded_pr_classes)
                    or "nothing",
                ),
            ),
            sources=(n.basis,),
            total=int(n.value),
            counted=sum(i.count for i in items),
            sections=(
                ModelFloorDetailSection(
                    heading="Open now, with the reason each waits",
                    note="Grouped by the watcher's class. The time is when it was opened.",
                    items=items,
                    empty="No in-scope pull request is open.",
                ),
            ),
        )

    def _slots(self) -> ModelFloorDetail:
        n = self.numbers["slots"]
        key, title = "number:slots", "Lab slots working"
        if not n.read or self.r.slots_working is None:
            return self._unread(key, title, n.label)
        by_host: dict[str, list[ModelFloorLane]] = {}
        for lane in self.r.lanes:
            by_host.setdefault(lane.host, []).append(lane)
        items: list[ModelFloorDetailItem] = []
        stale: list[ModelFloorDetailItem] = []
        for bay in self.r.bays:
            if bay.stale:
                stale.append(
                    ModelFloorDetailItem(
                        title=bay.host,
                        sub=f"Reading stale: its {bay.slots} slots are not in the total.",
                        count=0,
                        tone="amber",
                    )
                )
                continue
            group = f"{bay.host}: {bay.running} of {bay.slots} slots busy"
            lanes = sorted(
                by_host.get(bay.host, []),
                key=lambda ln: (ln.started_at is None, ln.started_at, ln.lane),
            )
            for lane in lanes:
                items.append(
                    ModelFloorDetailItem(
                        title=lane.lane,
                        group=group,
                        sub=" · ".join(
                            x
                            for x in (
                                lane.ticket,
                                "gone quiet" if lane.stalled else "",
                            )
                            if x
                        ),
                        href=self.link.ticket(lane.ticket) if lane.ticket else "",
                        at=lane.started_at,
                        tone="amber" if lane.stalled else None,
                    )
                )
            rest = bay.running - len(lanes)
            if rest > 0:
                items.append(
                    ModelFloorDetailItem(
                        title="Busy slots with no lane row",
                        group=group,
                        sub="Lab-run jobs and the landing workers' own leases: the slot marker does not say which.",
                        count=rest,
                    )
                )
            elif rest < 0:
                items.append(
                    ModelFloorDetailItem(
                        title="Lane rows beyond the host's busy slots",
                        group=group,
                        sub=f"The record lists {len(lanes)} lanes here and {bay.running} busy slots; the difference is taken off so the lines add up to the number.",
                        count=rest,
                        tone="amber",
                    )
                )
        unplaced = tuple(
            ModelFloorDetailItem(
                title=lane.lane,
                sub=lane.ticket or "",
                href=self.link.ticket(lane.ticket) if lane.ticket else "",
                at=lane.started_at,
                count=0,
            )
            for lane in by_host.get("", [])
        )
        counted = sum(i.count for i in items)
        sections = [
            ModelFloorDetailSection(
                heading="Busy slots, by host",
                note="Each lane is one slot; its start is the time it began.",
                items=tuple(items),
                empty="No slot is busy.",
            )
        ]
        if stale:
            sections.append(
                ModelFloorDetailSection(heading="Hosts not counted", items=tuple(stale))
            )
        if unplaced:
            sections.append(
                ModelFloorDetailSection(
                    heading="Lanes the record places on no host (not in the number)",
                    items=unplaced,
                )
            )
        return ModelFloorDetail(
            key=key,
            title=title,
            summary=n.label,
            tone="amber" if self.r.slots_unread_hosts else "green",
            facts=(
                ModelFloorDetailFact(label="Working", value=n.value),
                ModelFloorDetailFact(
                    label="Hosts not read",
                    value=", ".join(self.r.slots_unread_hosts) or "none",
                ),
            ),
            sources=(n.basis,),
            total=self.r.slots_working,
            counted=counted,
            sections=tuple(sections),
        )

    def _red_rows(self) -> ModelFloorDetail:
        rule = self.doc.red_rule
        items: list[ModelFloorDetailItem] = []
        for c in self.doc.causes:
            if c.listed and c.state == "red":
                items.append(
                    ModelFloorDetailItem(
                        title=c.title,
                        group="Holdups",
                        sub="It needs you." if c.needs_operator else "Nobody is on it.",
                        tone="red",
                        opens=f"cause:{c.id}",
                    )
                )
        for p in self.doc.pieces:
            if p.draw == "red":
                items.append(
                    ModelFloorDetailItem(
                        title=f"Piece {p.piece}, {p.name}",
                        group="Rebuild pieces",
                        sub=p.reason or p.text,
                        tone="red",
                        opens=f"piece:{p.piece}",
                    )
                )
        for line in self.doc.lab_state:
            if line.draw == "red":
                items.append(
                    ModelFloorDetailItem(
                        title=line.title,
                        group="Lab lines",
                        sub=line.text,
                        tone="red",
                        opens=f"lab:{line.id}",
                    )
                )
        return ModelFloorDetail(
            key="number:red_rows",
            title="Red rows",
            summary=(
                f"Colour rule {rule.state}: {rule.red_rows} red rows, "
                f"{rule.red_with_work_under_way} with work under way."
            ),
            tone="red" if rule.red_rows else "green",
            facts=(
                ModelFloorDetailFact(label="Red rows", value=str(rule.red_rows)),
                ModelFloorDetailFact(
                    label="With work under way",
                    value=str(rule.red_with_work_under_way),
                ),
            ),
            sources=(
                "The colour rule recomputed from the finished document and the lanes (check_red_rule).",
            ),
            total=rule.red_rows,
            counted=sum(i.count for i in items),
            sections=(
                ModelFloorDetailSection(
                    heading="Every red row",
                    note="Tap one to open its own details.",
                    items=tuple(items),
                    empty="No row is red.",
                ),
                ModelFloorDetailSection(
                    heading="Rule violations",
                    items=tuple(
                        ModelFloorDetailItem(title=v, tone="red", count=0)
                        for v in rule.violations
                    ),
                    empty="None: no red row has work under way.",
                ),
            ),
        )

    def _open_alarms(self) -> ModelFloorDetail:
        undelivered = tuple(a for a in self.doc.alarms if a.delivery != "delivered")
        return ModelFloorDetail(
            key="alarms:open",
            title="Lab alarms without a delivery receipt",
            summary=f"{len(undelivered)} of {len(self.doc.alarms)} open alarms have no delivery receipt.",
            tone="red" if undelivered else "green",
            sources=(
                "The alarm state of the lab-state alarm node: open alarms and their sends.",
            ),
            sections=(self._alarms(undelivered),),
        )

    # --- the rows ------------------------------------------------------------------------------------------------------

    def _cause_sources(self, cause: ModelFloorCause) -> tuple[str, ...]:
        cid = cause.id
        if cid.startswith("stoppage:"):
            built = self.r.friction_as_of
            return (
                "The friction rollup (stoppages in the factory floor record)"
                + (
                    f", built {_ago(max(int((self.r.now - built).total_seconds()), 0))}"
                    if built
                    else ""
                )
                + "; later rows are not in it.",
            )
        if cid.startswith("prs:"):
            return (
                f"The pull request watcher state, class {cid.removeprefix('prs:')}.",
            )
        if cid == "lab:state":
            return (
                "The lab sources read by the lab-state node; this row stands for its failing members.",
            )
        if cid.startswith("lab:"):
            v = self.verdicts.get(cid.removeprefix("lab:"))
            return (
                f"Lab source {cid.removeprefix('lab:')}"
                + (f" ({v.kind}), {v.reason}" if v else ""),
            )
        if cid == "chains:broken":
            return (
                "The emission receipts of the pieces' nodes (node_floor_emission_compute).",
            )
        if cid == "alarm:undelivered":
            return (
                "The alarm state: open alarms and whether any channel proved delivery.",
            )
        if cid.startswith("source:"):
            return (
                f"The floor's own input {cid.removeprefix('source:').replace('_', ' ')}, which could not be read.",
            )
        if cid.startswith("piece:"):
            return ("The piece's reading and its emission receipts.",)
        return ("The floor record.",)

    def _cause_alarm_ids(self, cause: ModelFloorCause) -> set[str]:
        if cause.id == "alarm:undelivered":
            return {a.source_id for a in self.doc.alarms if a.delivery == "undelivered"}
        ids = {cause.id.removeprefix("lab:")}
        ids |= {m.removeprefix("lab:") for m in cause.members}
        return ids

    def _cause(self, c: ModelFloorCause) -> ModelFloorDetail:
        if c.id.startswith("join:") and c.id.removeprefix("join:") in self._joins():
            # A join's red row opens the join's own panel under the row's key (OMN-23081).
            return self._join(self._joins()[c.id.removeprefix("join:")]).model_copy(
                update={"key": f"cause:{c.id}"}
            )
        why = {
            "red": "Red: it needs you."
            if c.needs_operator
            else "Red: no live lane is on it.",
            "amber": "Amber: a live lane is on it.",
            "quiet": "Quiet: its record says it is not occurring and nobody is needed.",
        }[c.state]
        facts = [
            ModelFloorDetailFact(label="State", value=why),
            ModelFloorDetailFact(label="Size", value=f"{c.size} {c.size_unit}"),
        ]
        if c.last_seen is not None:
            facts.append(
                ModelFloorDetailFact(label="Last seen", value="", at=c.last_seen)
            )
        if c.still_occurring is not None:
            facts.append(
                ModelFloorDetailFact(
                    label="Still occurring",
                    value="yes" if c.still_occurring else "no row since in the rollup",
                )
            )
        sections = [
            self._tickets(c.owners, c.tickets),
            self._lanes(c.lanes),
            self._prs(c.prs),
            self._alarms(self._alarms_for(self._cause_alarm_ids(c))),
        ]
        if c.members:
            sections.append(
                ModelFloorDetailSection(
                    heading="Lab sources it stands for",
                    items=tuple(
                        ModelFloorDetailItem(
                            title=self.causes[m].title,
                            sub=self.causes[m].detail,
                            tone=CAUSE_TONE[self.causes[m].state],
                            opens=f"cause:{m}",
                        )
                        for m in c.members
                        if m in self.causes
                    ),
                )
            )
        if c.id == "chains:broken":
            sections.append(
                ModelFloorDetailSection(
                    heading="Broken chains",
                    note="Each is a chain whose downstream piece gave no typed outcome in time.",
                    items=tuple(
                        ModelFloorDetailItem(
                            title=f"{f.subject}: {f.code}",
                            sub=f.detail,
                            tag=f.correlation_id[:8],
                            tone="red",
                        )
                        for f in self.doc.chain_findings
                    ),
                )
            )
        return ModelFloorDetail(
            key=f"cause:{c.id}",
            title=c.title,
            summary=c.detail,
            tone=CAUSE_TONE[c.state],
            facts=tuple(facts),
            sources=self._cause_sources(c),
            sections=tuple(sections),
        )

    def _lab(self, lab_id: str) -> ModelFloorDetail:
        line = next(x for x in self.doc.lab_state if x.id == lab_id)
        v = self.verdicts.get(lab_id)
        cause = self.causes.get(f"lab:{lab_id}")
        facts = [
            ModelFloorDetailFact(label="State", value=line.state),
            self._fact_time("Read", line.reading_age_s),
            ModelFloorDetailFact(
                label="May be at most", value=f"{line.max_age_s // 60} min old"
            ),
        ]
        sources = (
            (
                f"Lab source {lab_id} ({v.kind}), read by the lab-state node; "
                f"published {v.published_at.strftime('%Y-%m-%dT%H:%M:%SZ')}.",
            )
            if v
            else ("The lab-state node.",)
        )
        subjects = tuple(
            ModelFloorDetailItem(
                title=s.name,
                sub=" · ".join(
                    x
                    for x in (
                        "" if s.value is None else f"value {s.value:g}",
                        s.note,
                    )
                    if x
                ),
                tone=None if s.ok else "red",
                tag="ok" if s.ok else "failing",
            )
            for s in (v.subjects if v else ())
        )
        sections = [
            ModelFloorDetailSection(
                heading="What was read",
                note="One line per host, runner or item the source judges; a failing one is marked.",
                items=subjects,
                empty="The source returned no items.",
            ),
            self._tickets(
                cause.owners if cause else (), cause.tickets if cause else ()
            ),
            self._lanes(cause.lanes if cause else ()),
            self._alarms(self._alarms_for({lab_id})),
        ]
        return ModelFloorDetail(
            key=f"lab:{lab_id}",
            title=line.title,
            summary=line.text,
            tone=line.draw,
            facts=tuple(facts),
            sources=sources,
            sections=tuple(sections),
        )

    def _join(self, j: ModelFloorJoinRow) -> ModelFloorDetail:
        """What a join row opens (OMN-23081): its definition, its last run, the side that failed and the owner lane."""
        words, state_tone = JOIN_WORDS.get(j.state, (j.state, "grey"))
        cause = self.causes.get(f"join:{j.id}")
        tone: Draw = (
            CAUSE_TONE[cause.state]
            if cause is not None
            else ("green" if j.passes else state_tone)
        )
        pieces = {p.piece: p for p in self.doc.pieces}
        counts = (
            "yes: both of its pieces are in the goal's scope"
            if j.counted
            else "no: a piece of it is outside the goal's scope"
        )
        facts = [
            ModelFloorDetailFact(label="Definition", value=j.definition),
            ModelFloorDetailFact(label="State", value=f"{j.id} {words}"),
            ModelFloorDetailFact(label="Counts toward ready", value=counts),
            self._ran("Last run", j.last_run_age_s),
            self._ran("Golden chain", j.golden_age_s),
            self._ran("Error chain", j.error_age_s),
        ]
        if not j.passes:
            side = {
                "from": f"the from side, piece {j.from_piece}, {pieces[j.from_piece].name.lower()}",
                "to": f"the to side, piece {j.to_piece}, {pieces[j.to_piece].name.lower()}",
                "neither": "neither side alone: both pieces read, no cycle ran the join",
            }.get(j.failed_side, "not known")
            facts.append(ModelFloorDetailFact(label="Failed side", value=side))
            facts.append(
                ModelFloorDetailFact(label="Why", value=j.failed_reason or "no reason")
            )
        findings = [
            f for f in self.doc.chain_findings if f.subject.startswith(f"{j.id} piece ")
        ]
        sections = [
            ModelFloorDetailSection(
                heading="The two pieces",
                note="The upstream piece and the downstream piece; each opens its own reading.",
                items=tuple(
                    ModelFloorDetailItem(
                        title=f"Piece {n}, {pieces[n].name}",
                        sub=f"{role}: {pieces[n].text}",
                        tag=role,
                        tone=pieces[n].draw,
                        opens=f"piece:{n}",
                    )
                    for role, n in (("from", j.from_piece), ("to", j.to_piece))
                ),
            ),
            ModelFloorDetailSection(
                heading="Chain findings",
                note="Chains of this join that break the emission contract.",
                items=tuple(
                    ModelFloorDetailItem(
                        title=f.code,
                        sub=f.detail,
                        tag=f.correlation_id[:8],
                        tone="red",
                    )
                    for f in findings
                ),
                empty="No chain of this join breaks the contract.",
            ),
        ]
        if j.counted:
            sections += [
                self._tickets(j.owners, ()),
                ModelFloorDetailSection(
                    heading="Owner lane",
                    note="The live lanes on the owner tickets; the row is red until one is.",
                    items=self._lane_items(j.lanes),
                    empty="No live lane is on it."
                    if j.owners
                    else "No owner ticket is declared, so no lane can be on it.",
                ),
            ]
        return ModelFloorDetail(
            key=f"join:{j.id}",
            title=f"Join {j.id}, {j.label}",
            summary=f"{j.definition}. {j.id} {words}.",
            tone=tone,
            facts=tuple(facts),
            sources=(
                "The emission receipts: the golden and the error chain of the two pieces under one correlation id.",
                "The floor overlay's readiness scope, which says which joins count toward ready.",
            ),
            sections=tuple(sections),
        )

    def _ran(self, label: str, age_s: int | None) -> ModelFloorDetailFact:
        if age_s is None:
            return ModelFloorDetailFact(label=label, value="never run")
        return ModelFloorDetailFact(
            label=label, value="", at=self.r.now - timedelta(seconds=age_s)
        )

    def _join_items(self, piece: int) -> tuple[ModelFloorDetailItem, ...]:
        receipts = {j.join: j for j in self.r.receipts.joins} if self.r.receipts else {}
        out: list[ModelFloorDetailItem] = []
        for j in self.r.overlay.joins:
            if piece not in (j.from_piece, j.to_piece):
                continue
            rc = receipts.get(j.id)
            state = rc.state if rc else "unread"
            words, tone = JOIN_WORDS.get(state, (state, "grey"))
            ages = []
            if rc is not None and rc.golden_at is not None:
                ages.append(
                    "golden "
                    + _ago(max(int((self.r.now - rc.golden_at).total_seconds()), 0))
                )
            if rc is not None and rc.error_at is not None:
                ages.append(
                    "error "
                    + _ago(max(int((self.r.now - rc.error_at).total_seconds()), 0))
                )
            out.append(
                ModelFloorDetailItem(
                    title=f"{j.id} {j.label}",
                    sub=f"Piece {j.from_piece} to piece {j.to_piece} {words}"
                    + (f" ({', '.join(ages)})" if ages else ""),
                    tag="out" if j.from_piece == piece else "in",
                    tone=tone,
                    opens=f"join:{j.id}",
                )
            )
        return tuple(out)

    def _piece(self, p: ModelFloorPieceRow) -> ModelFloorDetail:
        cids = [x for x in (p.cause_id, f"piece:{p.piece}") if x]
        causes = [self.causes[c] for c in dict.fromkeys(cids) if c in self.causes]
        owners = tuple(dict.fromkeys(t for c in causes for t in c.owners))
        seen = tuple(dict.fromkeys(t for c in causes for t in c.tickets))
        lanes = tuple(dict.fromkeys(x for c in causes for x in c.lanes))
        prs = tuple(dict.fromkeys(x for c in causes for x in c.prs))
        ov = self.r.overlay
        lab_ids = set(ov.rebuild.piece_lab_sources.get(p.piece, ()))
        if p.piece == ov.rebuild.lab_state_piece:
            lab_ids |= set(self.verdicts)
        sources = [
            "The piece's reading row (the reading writer) and the emission receipts of its nodes."
        ]
        if lab_ids:
            sources.append(
                "Lab sources it stands on: " + ", ".join(sorted(lab_ids)) + "."
            )
        facts = [
            ModelFloorDetailFact(label="Status", value=p.status.replace("_", " ")),
            ModelFloorDetailFact(
                label="Reading", value=p.reading_state or "no reading"
            ),
            self._fact_time("Read", p.reading_age_s),
            ModelFloorDetailFact(
                label="May be at most", value=f"{p.reading_max_age_s // 60} min old"
            ),
        ]
        joins = ModelFloorDetailSection(
            heading="Join checks",
            note="Joins that leave or enter this piece: a join passes when a golden and an error chain both ended as the contract says.",
            items=self._join_items(p.piece),
            empty="No join is declared for this piece.",
        )
        # A piece that has not started has no owner, lane, pull request or alarm to show: only its declared joins.
        sections = (
            [joins]
            if p.status == "not_started"
            else [
                self._tickets(owners, seen),
                self._lanes(lanes),
                self._prs(prs),
                self._alarms(self._alarms_for(lab_ids)),
                joins,
            ]
        )
        if p.shadows:
            sections.append(
                ModelFloorDetailSection(
                    heading="Shadow checks",
                    note="What the piece's checks would have refused; they hold no merge.",
                    items=tuple(
                        ModelFloorDetailItem(
                            title=s.label,
                            sub=s.text,
                            at=self.r.now - timedelta(seconds=s.age_s),
                            tone="amber" if s.stale else None,
                            tag="stale" if s.stale else "",
                        )
                        for s in p.shadows
                    ),
                )
            )
        return ModelFloorDetail(
            key=f"piece:{p.piece}",
            title=f"Piece {p.piece}, {p.name}",
            summary=p.reason or p.text,
            tone=p.draw,
            facts=tuple(facts),
            sources=tuple(sources),
            sections=tuple(sections),
        )
