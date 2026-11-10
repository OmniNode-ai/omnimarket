# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Readers of the sources the 17a floor draws from (OMN-19998).

Each reader returns what was read, with the time of the read, or says it could not read: it never returns a zero or an
empty value for a source it could not read. Paths are the contract's; limits and class names are the overlay's.
"""

from __future__ import annotations

import contextlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any

from omnimarket.models.floor_17a import (
    ModelFloorCauseIn,
    ModelFloorLane,
    ModelGoalScope,
    ModelLabSourceDef,
    ModelLabSourceRead,
    ModelLabStateOverlay,
    ModelLabSubject,
    ModelNodeEmission,
    ModelRunnerPlanRule,
)
from omnimarket.models.floor_17a.model_delegation_idle import (
    ModelDelegationIdleOverlay,
    ModelDelegationTicketIn,
)
from omnimarket.models.work_state_projection import (
    EnumCriterionReading,
    EnumWorkItemKind,
    EnumWorkItemState,
    ModelWorkStateResult,
)

TICKET = re.compile(r"\bOMN-\d+\b")
SIZE_UNITS = {"K": 1 / 1024 / 1024, "M": 1 / 1024, "G": 1.0, "T": 1024.0}
TAIL_BYTES = 400_000
CACHED_CELL = re.compile(r"\bcached=(\d+(?:\.\d+)?)s\b")
# No admission record given: the lists alone decide. ``None`` instead is a record that could not be read.
NO_ADMISSIONS: Mapping[str, Mapping[str, Any]] = MappingProxyType({})


class FloorSourceError(Exception):
    """A source could not be read; ``code`` is the typed failure code the emission carries."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def parse_time(text: object) -> datetime:
    if not isinstance(text, str) or not text:
        raise ValueError("no time")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def tickets_of(*texts: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(t for text in texts for t in TICKET.findall(text)))


# --- the factory floor record ------------------------------------------------------------------------------------------


def load_record(raw: bytes) -> dict[str, Any]:
    """The floor record: an object with ``generated_at`` and ``floor``. Anything else is a typed failure."""
    try:
        record = json.loads(raw)
    except ValueError as exc:
        raise FloorSourceError(
            "record_payload_invalid",
            f"the floor record is not JSON ({type(exc).__name__})",
        ) from exc
    if not isinstance(record, dict) or not isinstance(record.get("floor"), dict):
        raise FloorSourceError(
            "record_payload_invalid", "the floor record has no floor object"
        )
    try:
        parse_time(record.get("generated_at"))
    except ValueError as exc:
        raise FloorSourceError(
            "record_payload_invalid", "the floor record carries no generated_at"
        ) from exc
    return record


def read_record(path: Path) -> dict[str, Any] | None:
    try:
        return load_record(path.read_bytes())
    except OSError:
        return None
    except FloorSourceError:
        return None


def record_lanes(record: Mapping[str, Any]) -> tuple[ModelFloorLane, ...]:
    lanes = record["floor"].get("lanes", [])
    return tuple(
        ModelFloorLane(
            lane=str(item.get("lane", "")),
            ticket=str(item.get("ticket") or ""),
            stalled=bool(item.get("stalled", False)),
            pr=str(item.get("pr") or ""),
            liveness=str(item.get("liveness") or "unread"),
            owns=tuple(str(o) for o in item.get("owns") or () if o),
            parent_ticket=str(item.get("parent_ticket") or ""),
        )
        for item in lanes
        if isinstance(item, dict) and item.get("lane")
    )


# A lane item's attributes are the aliases its CLAIM cells name; these are the prefixes a floor row can be named by.
_CAUSE_PREFIX = "cause:"
_OWNS_PREFIX = "pr:floor:"
_TICKET_PREFIX = "ticket:"
_PR_PREFIX = "pr:"
_LIVENESS_OF_STATE = {
    EnumWorkItemState.IN_PROGRESS: "unread",
    EnumWorkItemState.STOPPED: "dead",
    EnumWorkItemState.DONE: "finished",
}


def read_work_state(path: Path) -> ModelWorkStateResult | None:
    """The work-state projection as the projection node wrote it, or None when it is absent or not a projection."""
    try:
        return ModelWorkStateResult.model_validate_json(path.read_bytes())
    except (OSError, ValueError):
        return None


def read_delegation_tickets(
    projection: ModelWorkStateResult | None, spec: ModelDelegationIdleOverlay
) -> tuple[ModelDelegationTicketIn, ...] | None:
    """The operator-owned delegation tickets whose start criterion reads true and that are not done (OMN-23313).

    A ticket is delegation work when its parent chain reaches one of the overlay's epics (an epic is not its own member),
    and the operator's when its tracker assignee is one of the overlay's operator assignees. ``None`` when the projection
    is absent or no ticket in it carries an assignee at all: the tracker snapshot was the compact form, ownership was not
    read, and an empty list there would call every ticket nobody's.
    """
    if projection is None:
        return None
    tickets = {
        i.ticket: i
        for i in projection.items
        if i.kind is EnumWorkItemKind.TICKET and i.ticket
    }
    if not any(i.assignee for i in tickets.values()):
        return None
    epics = set(spec.epics)
    operators = set(spec.operator_assignees)

    def under_epic(parent: str | None) -> bool:
        seen: set[str] = set()
        while parent and parent not in seen:
            if parent in epics:
                return True
            seen.add(parent)
            parent = tickets[parent].parent if parent in tickets else None
        return False

    return tuple(
        ModelDelegationTicketIn(
            ticket=i.ticket or "", title=i.title, assignee=i.assignee or ""
        )
        for _, i in sorted(tickets.items())
        if i.assignee in operators
        and i.state is not EnumWorkItemState.DONE
        and i.criterion is EnumCriterionReading.TRUE
        and under_epic(i.parent)
    )


def work_state_lanes(projection: ModelWorkStateResult) -> tuple[ModelFloorLane, ...]:
    """Each projected lane as the floor draws it, attributed to rows by the cells of its CLAIM alone.

    A ``cause=<row id>`` cell and an ``owns=floor:<row id>`` cell name a floor row; the ``ticket`` and ``owns`` pull
    request cells give the lane's ticket and pull request. No owner is read from any overlay.
    """
    lanes: list[ModelFloorLane] = []
    for item in projection.items:
        if item.kind is not EnumWorkItemKind.LANE:
            continue
        name = item.key.removeprefix("lane:")
        attrs = item.attributes
        owns = tuple(
            dict.fromkeys(
                [
                    a.removeprefix(_CAUSE_PREFIX)
                    for a in attrs
                    if a.startswith(_CAUSE_PREFIX)
                ]
                + [
                    a.removeprefix(_OWNS_PREFIX)
                    for a in attrs
                    if a.startswith(_OWNS_PREFIX)
                ]
            )
        )
        ticket = next(
            (
                a.removeprefix(_TICKET_PREFIX)
                for a in attrs
                if a.startswith(_TICKET_PREFIX)
            ),
            "",
        )
        pr = next(
            (
                a.removeprefix(_PR_PREFIX)
                for a in attrs
                if a.startswith(_PR_PREFIX)
                and not a.startswith(_OWNS_PREFIX)
                and "#" in a
            ),
            "",
        )
        lanes.append(
            ModelFloorLane(
                lane=name,
                ticket=ticket,
                stalled=False,
                pr=pr,
                liveness=_LIVENESS_OF_STATE[item.state],
                owns=owns,
            )
        )
    return tuple(lanes)


def merge_lanes(
    record: tuple[ModelFloorLane, ...], projected: tuple[ModelFloorLane, ...]
) -> tuple[ModelFloorLane, ...]:
    """The record's lanes keep their process reading; the projection adds what its CLAIM cells say, and any lane the
    record does not list."""
    by_name = {lane.lane: lane for lane in projected}
    merged: list[ModelFloorLane] = []
    for lane in record:
        extra = by_name.pop(lane.lane, None)
        if extra is None:
            merged.append(lane)
            continue
        merged.append(
            lane.model_copy(
                update={
                    "ticket": lane.ticket or extra.ticket,
                    "pr": lane.pr or extra.pr,
                    "owns": tuple(dict.fromkeys((*lane.owns, *extra.owns))),
                }
            )
        )
    merged.extend(by_name.values())
    return tuple(merged)


def record_stoppages(record: Mapping[str, Any]) -> tuple[ModelFloorCauseIn, ...]:
    out: list[ModelFloorCauseIn] = []
    for item in record["floor"].get("stoppages", []):
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"])
        try:
            last_seen: datetime | None = parse_time(item.get("last_seen"))
        except ValueError:
            last_seen = None
        try:
            resolved_at: datetime | None = (
                parse_time(item["resolved_at"]) if item.get("resolved_at") else None
            )
        except ValueError:
            resolved_at = None
        cost = item.get("cost_minutes")
        out.append(
            ModelFloorCauseIn(
                id=f"stoppage:{name}",
                title=name.replace("-", " ").replace(":", ": "),
                detail=f"{item.get('occurrences', 0)} {'time' if item.get('occurrences') == 1 else 'times'}",
                size=int(item.get("occurrences", 0)),
                size_unit="occurrences",
                tickets=tuple(str(t) for t in item.get("tickets", [])),
                last_seen=last_seen,
                resolved_at=resolved_at,
                reported_cost_minutes=float(cost)
                if isinstance(cost, int | float)
                else None,
            )
        )
    return tuple(out)


def record_slots(
    record: Mapping[str, Any],
) -> tuple[int | None, int | None, tuple[str, ...]]:
    """Running and total slots of the read hosts, and the hosts whose bay is stale."""
    working = total = 0
    unread: list[str] = []
    bays = record["floor"].get("bays", [])
    for bay in bays:
        if not isinstance(bay, dict):
            continue
        if bay.get("stale"):
            unread.append(str(bay.get("host", "?")))
            continue
        working += int(bay.get("running", 0))
        total += int(bay.get("slots", 0))
    if not bays:
        return None, None, ()
    return working, total, tuple(unread)


# --- the pull request watcher ------------------------------------------------------------------------------------------


def read_admissions(path: Path, tail_bytes: int) -> dict[str, Mapping[str, Any]] | None:
    """The landing controller's newest admission row per pull request (``repo#number`` to row), or ``None`` when the record
    cannot be read (OMN-22876).

    The controller appends one ``admission`` row each time its verdict on a pull request is new or changed; the last row of a
    pull request wins and only the newest ``tail_bytes`` of the file are read. A record not written yet is empty: nothing was
    admitted off the goal lists. One that cannot be read is ``None``, never an empty record.
    """
    try:
        with path.open("rb") as handle:
            size = handle.seek(0, 2)
            handle.seek(max(0, size - tail_bytes))
            tail = handle.read().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    rows: dict[str, Mapping[str, Any]] = {}
    for line in tail.splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if (
            isinstance(row, dict)
            and row.get("kind") == "admission"
            and row.get("event") in ("admitted", "kept_out")
            and row.get("pr")
        ):
            rows[str(row["pr"])] = row
    return rows


@dataclass(frozen=True)
class WatcherReading:
    """The pull request watcher, read against the landing goal's scope.

    ``scoped`` is false when the goal scope could not be read: the in-scope counts are then ``None`` and the queue ages
    are empty, because an unread scope is never everything in scope. The ``*_outside`` counts are the pull requests the
    goal does not count, kept so the page can say how many sit outside.
    """

    last_tick: datetime
    scoped: bool
    open_prs: int | None
    merges_in_window: int | None
    open_outside: int | None = None
    merges_outside: int | None = None
    # The merges counted in scope only because the landing controller admitted them (OMN-22876), as ``repo#number``.
    merges_controller: tuple[str, ...] = ()
    paused_open: int | None = None
    class_counts: dict[str, int] = field(default_factory=dict)
    class_tickets: dict[str, tuple[str, ...]] = field(default_factory=dict)
    class_prs: dict[str, tuple[str, ...]] = field(default_factory=dict)
    queue_ages: tuple[tuple[str, int], ...] = ()
    # The queue-age pull requests whose branch conflicts: green checks, but they cannot be armed.
    queue_conflicted: frozenset[str] = frozenset()


def read_watcher(
    path: Path,
    now: datetime,
    *,
    excluded_classes: tuple[str, ...],
    queue_classes: tuple[str, ...],
    merge_window_minutes: int,
    scope: ModelGoalScope | None,
    conflict_states: tuple[str, ...] = (),
    admissions: Mapping[str, Mapping[str, Any]] | None = NO_ADMISSIONS,
    projected: frozenset[str] = frozenset(),
) -> WatcherReading | None:
    """``projected`` holds the pull requests (``repo#n``) the work-state projection keeps in scope through the goal's ticket
    rule. The goal scope's lists name 58 pull requests and the rule admits the rest by ticket, so reading the lists alone left
    the red class holding the 2 listed ones while the controller counted 60 to 70 (OMN-23184)."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        last_tick = parse_time(data["last_tick"])
        prs = data["prs"]
        merges = data.get("merges", {})
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if scope is None:
        return WatcherReading(
            last_tick=last_tick, scoped=False, open_prs=None, merges_in_window=None
        )
    counts: dict[str, int] = {}
    tickets: dict[str, list[str]] = {}
    class_prs: dict[str, list[str]] = {}
    open_prs = outside = paused = 0
    ages: list[tuple[str, int]] = []
    conflicted: set[str] = set()
    for key, value in prs.items():
        facts = value.get("facts", {})
        if facts.get("state") != "OPEN":
            continue
        where = scope.classify(
            str(key),
            str(facts.get("title", "")),
            str(facts.get("head_ref", "")),
            str(facts.get("body", "")),
        )
        if where == "unlisted" and str(key) in projected:
            where = "in_scope"
        if where != "in_scope":
            outside += 1
            paused += where == "paused"
            continue
        cls = str(value.get("cls", ""))
        if cls not in excluded_classes:
            open_prs += 1
        counts[cls] = counts.get(cls, 0) + 1
        tickets.setdefault(cls, []).extend(
            tickets_of(str(facts.get("title", "")), str(facts.get("head_ref", "")))
        )
        class_prs.setdefault(cls, []).append(str(key))
        if cls in queue_classes and not facts.get("draft", False):
            done = [
                parse_time(d[2])
                for d in (value.get("ci") or {}).get("detail", [])
                if isinstance(d, list) and len(d) > 2 and d[2]
            ]
            if done:
                # OMN-23056: the wait starts when the pull request could be landed. Checks that finished while it was a
                # draft are older than that, so a handoff to landing for this very head moves the start forward to it.
                started = max(done)
                handoff = value.get("handoff")
                if (
                    isinstance(handoff, dict)
                    and handoff.get("at")
                    and handoff.get("head") == facts.get("head_sha")
                ):
                    with contextlib.suppress(ValueError):
                        started = max(started, parse_time(handoff["at"]))
                ages.append((key, max(int((now - started).total_seconds() // 60), 0)))
                merge_state = str((value.get("review") or {}).get("merge_state") or "")
                if merge_state in conflict_states:
                    conflicted.add(str(key))
    cutoff = now - timedelta(minutes=merge_window_minutes)
    recent = recent_outside = 0
    by_controller: list[str] = []
    for key, value in merges.items():
        try:
            merged_at = parse_time(value.get("merged_at"))
        except ValueError:
            continue
        if merged_at < cutoff:
            continue
        where = scope.classify(
            str(key), str(value.get("title", "")), str(value.get("head_ref", ""))
        )
        # OMN-22876: a merge on no list that the controller's newest admission row admitted counts in scope. The paused list
        # overrides, as it does for the controller, which never admits a paused pull request.
        row = (admissions or {}).get(str(key))
        if where == "unlisted" and row is not None and row.get("admitted") is True:
            where = "in_scope"
            by_controller.append(str(key))
        if where == "in_scope":
            recent += 1
        else:
            recent_outside += 1
    return WatcherReading(
        last_tick=last_tick,
        scoped=True,
        open_prs=open_prs,
        merges_in_window=recent if admissions is not None else None,
        open_outside=outside,
        merges_outside=recent_outside,
        merges_controller=tuple(sorted(by_controller)),
        paused_open=paused,
        class_counts=counts,
        class_tickets={k: tuple(dict.fromkeys(v)) for k, v in tickets.items()},
        class_prs={k: tuple(dict.fromkeys(v)) for k, v in class_prs.items()},
        queue_ages=tuple(sorted(ages, key=lambda a: -a[1])),
        queue_conflicted=frozenset(conflicted),
    )


# --- the lab sources ---------------------------------------------------------------------------------------------------


def _unread(source_id: str, reason: str) -> ModelLabSourceRead:
    return ModelLabSourceRead(id=source_id, status="unread", reason=reason)


def _by_kind(overlay: ModelLabStateOverlay, kind: str) -> ModelLabSourceDef:
    return next(s for s in overlay.sources if s.kind == kind)


def _busy_with_own_units(reason: str, overlay: ModelLabStateOverlay) -> bool:
    """True when the pool's refusal is the load bar alone and the units placed on the host explain that load (OMN-22870).

    The placement prints the own-load part only when the load bar refuses and the load less the cost of the units placed
    there is inside the bar. A host full of our own work refuses new work; it is not failing. Any other refusal part,
    and a load nothing placed explains (no own-load part), is not this case.
    """
    marker = overlay.own_load_marker
    if not marker:
        return False
    parts = [p.strip() for p in reason.split(overlay.refusal_separator) if p.strip()]
    if not any(p.startswith(marker) for p in parts):
        return False
    allowed = (marker, *overlay.load_bar_markers)
    return all(p.startswith(allowed) for p in parts)


def read_hosts(
    record: Mapping[str, Any] | None, overlay: ModelLabStateOverlay
) -> ModelLabSourceRead:
    source = _by_kind(overlay, "hosts")
    if record is None:
        return _unread(source.id, "the floor record is not read, so no host is")
    machines = [m for m in record["floor"].get("machines", []) if isinstance(m, dict)]
    if not machines:
        return _unread(source.id, "the floor record lists no machines")
    subjects: list[ModelLabSubject] = []
    read_times: list[datetime] = []
    for m in machines:
        host, state = str(m.get("host", "?")), str(m.get("state", ""))
        if state in overlay.declared_host_states:
            note = str(m.get("note", ""))[:90]
            subjects.append(
                ModelLabSubject(name=host, ok=True, note=f"{state}: {note}")
            )
            continue
        try:
            read_times.append(parse_time(m.get("read_at")))
        except ValueError:
            subjects.append(ModelLabSubject(name=host, ok=False, note="unread"))
            continue
        admission = m.get("admission") or {}
        reason = str(admission.get("reason", ""))
        refused = admission.get("state") == "refused"
        if refused and _busy_with_own_units(reason, overlay):
            subjects.append(
                ModelLabSubject(
                    name=host,
                    ok=True,
                    note=f"busy with its own placed units: {reason}"[:140],
                )
            )
            continue
        subjects.append(
            ModelLabSubject(
                name=host,
                ok=not refused,
                note=reason[:140] if refused else "advertising",
            )
        )
    working = sum(1 for s in subjects if s.ok)
    return ModelLabSourceRead(
        id=source.id,
        status="read",
        read_at=min(read_times) if read_times else None,
        summary=f"{working} of {len(subjects)} hosts fine",
        subjects=tuple(subjects),
    )


def _fleet_rollup(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The groups of a fleet document: its own class rollup, or one group per host from the runner rows it carries.

    The floor's fleet source saves each runner as a host and a status. A runner that is not offline is up; a busy
    one is up and working, so it counts online as well as busy, as the projection's own rollup counts it.
    """
    if "class_rollup" in data:
        return [dict(g) for g in data["class_rollup"]]
    groups: dict[str, dict[str, Any]] = {}
    for row in data["runners"]:
        name = str(row["host"])
        group = groups.setdefault(
            name, {"label_class": name, "online": 0, "busy": 0, "offline": 0}
        )
        status = str(row["status"])
        if status == "offline":
            group["offline"] += 1
        else:
            group["online"] += 1
            if status == "busy":
                group["busy"] += 1
    return list(groups.values())


def _plan_rule(name: str, overlay: ModelLabStateOverlay) -> ModelRunnerPlanRule | None:
    """The rule whose prefix is the longest one that starts the runner's name."""
    named = [r for r in overlay.runner_plan if name.startswith(r.name_prefix)]
    return max(named, key=lambda r: len(r.name_prefix)) if named else None


def _declared_machines(
    record: Mapping[str, Any] | None, overlay: ModelLabStateOverlay
) -> dict[str, str]:
    """Machines of the floor record that the host table declares out or in repair, with the table's note."""
    if record is None:
        return {}
    machines = record.get("floor", {}).get("machines", [])
    return {
        str(m.get("host")): str(m.get("note", ""))
        for m in machines
        if isinstance(m, dict) and m.get("state") in overlay.declared_host_states
    }


@dataclass
class _OfflineSplit:
    """Offline runners by what the host table and the plan say of them. Only ``needs_repair`` and ``unsplit`` are red."""

    parked_host: dict[str, list[str]] = field(default_factory=dict)
    parked_plan: dict[str, list[str]] = field(default_factory=dict)
    needs_repair: list[tuple[str, str]] = field(default_factory=list)
    unsplit: int = 0


def _split_offline(
    rows: list[Any], overlay: ModelLabStateOverlay, declared: Mapping[str, str]
) -> _OfflineSplit:
    split = _OfflineSplit()
    for row in rows:
        if not isinstance(row, dict) or row.get("status") != "offline":
            continue
        name = row.get("runner_name")
        if not name:
            split.unsplit += 1
            continue
        rule = _plan_rule(str(name), overlay)
        if rule is None:
            split.needs_repair.append(
                (str(name), "offline, and no host table or plan row names it")
            )
        elif rule.host in declared:
            split.parked_host.setdefault(rule.host, []).append(str(name))
        elif not rule.meant_to_run:
            split.parked_plan.setdefault(rule.plan, []).append(str(name))
        else:
            split.needs_repair.append(
                (
                    str(name),
                    f"offline, meant to run ({rule.plan})"
                    + (f"; restored by: {rule.restore}" if rule.restore else ""),
                )
            )
    return split


def read_runner_groups(
    path: Path,
    overlay: ModelLabStateOverlay,
    record: Mapping[str, Any] | None = None,
) -> ModelLabSourceRead:
    source = _by_kind(overlay, "runner_groups")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return _unread(
            source.id, f"the runner fleet file cannot be opened ({type(exc).__name__})"
        )
    except ValueError:
        return _unread(source.id, "the runner fleet file is not JSON")
    if isinstance(data, dict) and data.get("status") == "unread":
        return _unread(
            source.id, str(data.get("reason") or "fleet producer unread")[:200]
        )
    try:
        observed = parse_time(data["observed_at"])
        rollup = _fleet_rollup(data)
        rows = data.get("runners", [])
    except (KeyError, ValueError, TypeError):
        return _unread(
            source.id,
            "the runner fleet file has no observed_at, runners or class_rollup",
        )
    versions: dict[str, set[str]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("version"):
            versions.setdefault(str(row.get("label_class", "")), set()).add(
                str(row["version"])
            )
    limit = int(source.limit) if source.limit is not None else 0
    declared = _declared_machines(record, overlay)
    split = _split_offline(rows, overlay, declared)
    subjects: list[ModelLabSubject] = []
    for group in rollup:
        name = str(group.get("label_class", "?"))
        offline = int(group.get("offline", 0))
        seen = versions.get(name)
        version = ", ".join(sorted(seen)) if seen else "version not reported"
        group_rows = [
            r for r in rows if isinstance(r, dict) and str(r.get("host")) == name
        ]
        own = _split_offline(group_rows, overlay, declared)
        real = len(own.needs_repair) + own.unsplit
        parked = sum(len(v) for v in own.parked_host.values()) + sum(
            len(v) for v in own.parked_plan.values()
        )
        ok = (real if group_rows else offline) <= limit and not (
            overlay.require_runner_version and not seen
        )
        note = f"{group.get('online', 0)} online, {offline} offline, {version}"
        if parked:
            note += f"; {parked} of the offline are parked on purpose, {real} are not"
        subjects.append(
            ModelLabSubject(
                name=name, ok=ok, value=float(group.get("online", 0)), note=note
            )
        )
    for host, names in sorted(split.parked_host.items()):
        subjects.append(
            ModelLabSubject(
                name=f"{host} runners",
                ok=True,
                value=float(len(names)),
                note=f"{len(names)} runners offline, parked on purpose: {_declared_machines(record, overlay).get(host, '')[:90]}",
            )
        )
    for plan, names in sorted(split.parked_plan.items()):
        subjects.append(
            ModelLabSubject(
                name=", ".join(sorted(names)[:3]),
                ok=True,
                value=float(len(names)),
                note=f"{len(names)} runners offline, left stopped by the plan ({plan})",
            )
        )
    for runner, why in split.needs_repair:
        subjects.append(
            ModelLabSubject(name=runner, ok=False, value=0.0, note=why[:240])
        )
    if split.unsplit:
        subjects.append(
            ModelLabSubject(
                name="unnamed runners",
                ok=False,
                value=float(split.unsplit),
                note=f"{split.unsplit} offline runners carry no name in the saved fleet file, so they cannot be told apart; the producer that saves it is older than the one that adds names",
            )
        )
    return ModelLabSourceRead(
        id=source.id,
        status="read",
        read_at=observed,
        summary=f"{len(rollup)} runner groups read",
        subjects=tuple(subjects),
    )


def _free_gb(entry: str, key: str) -> float | None:
    match = re.search(rf"\b{re.escape(key)}=([\d.]+)([KMGT])/", entry)
    return float(match.group(1)) * SIZE_UNITS[match.group(2)] if match else None


def _cached_s(rest: str) -> float:
    """Seconds the host's cell text was already old when the receipt was written (the probe's ``cached=<n>s`` cell), else 0."""
    match = CACHED_CELL.search(rest)
    return float(match.group(1)) if match else 0.0


def disk_expected_hosts(
    record: Mapping[str, Any] | None, overlay: ModelLabStateOverlay
) -> tuple[str, ...]:
    """The hosts the pool receipt must hold a disk line for: every machine of the floor record that is not declared out or
    in repair and that the overlay does not list as unprobed. A host the receipt lacks is unread, never absent."""
    if record is None:
        return ()
    machines = [m for m in record["floor"].get("machines", []) if isinstance(m, dict)]
    return tuple(
        host
        for m in machines
        if (host := str(m.get("host", "")))
        and str(m.get("state", "")) not in overlay.declared_host_states
        and host not in overlay.disk_unprobed_hosts
    )


def read_disk(
    path: Path,
    overlay: ModelLabStateOverlay,
    now: datetime | None = None,
    expected_hosts: Sequence[str] = (),
) -> ModelLabSourceRead:
    """A host is above the bar only when every disk figure the overlay names is read, no older than the source's bound
    and at or over the bar. A figure that is unread, a host the receipt holds no line for, and a figure older than its
    bound count as not above the bar (OMN-23224); the bar is the overlay's and is not changed here."""
    source = _by_kind(overlay, "disk")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        started = parse_time(data["started_at"])
        readings = data["readings"]
    except OSError as exc:
        return _unread(
            source.id, f"the pool receipt cannot be opened ({type(exc).__name__})"
        )
    except (ValueError, KeyError, TypeError):
        return _unread(source.id, "the pool receipt has no started_at or readings")
    floor = source.limit if source.limit is not None else 0.0
    subjects: list[ModelLabSubject] = []
    for entry in readings:
        host, _, rest = str(entry).partition(":")
        figures = {k: _free_gb(rest, k) for k in overlay.disk_keys}
        free = [f for f in figures.values() if f is not None]
        unread = [k for k, f in figures.items() if f is None]
        age_s = _cached_s(rest) + (
            max((now - started).total_seconds(), 0.0) if now is not None else 0.0
        )
        low = min(free) if free else None
        why: list[str] = []
        if unread:
            why.append(f"{', '.join(unread)} unread in the pool receipt")
        if low is not None and low < floor:
            why.append(f"{low:.1f} GB free, under the {floor:g} GB floor")
        if age_s > source.max_age_s:
            why.append(
                f"reading {age_s:.0f} s old, over the {source.max_age_s} s bound"
            )
        subjects.append(
            ModelLabSubject(
                name=host,
                ok=not why,
                value=None if low is None else round(low, 1),
                note="; ".join(why) if why else f"{low:.1f} GB free",
            )
        )
    held = {s.name for s in subjects}
    subjects.extend(
        ModelLabSubject(
            name=missing,
            ok=False,
            note="no line in the pool receipt, so its disk is unread",
        )
        for missing in expected_hosts
        if missing not in held
    )
    if not subjects:
        return _unread(source.id, "the pool receipt holds no host")
    low_hosts = [s.name for s in subjects if not s.ok]
    return ModelLabSourceRead(
        id=source.id,
        status="read",
        read_at=started,
        summary=f"{len(subjects) - len(low_hosts)} of {len(subjects)} hosts above {floor:g} GB free",
        subjects=tuple(subjects),
    )


def read_queue_age(
    watcher: WatcherReading | None, overlay: ModelLabStateOverlay
) -> ModelLabSourceRead:
    source = _by_kind(overlay, "queue_age")
    if watcher is None:
        return _unread(source.id, "the pull request watcher is not read")
    if not watcher.scoped:
        return _unread(
            source.id,
            "the landing goal's scope is not read, so no pull request can be judged in or out of it",
        )
    limit = source.limit if source.limit is not None else 0.0
    oldest = watcher.queue_ages[:3]
    subjects = [
        ModelLabSubject(
            name=key,
            ok=age <= limit,
            value=float(age),
            note=(
                f"checks green {age} min ago but the branch conflicts, so it cannot be armed"
                if key in watcher.queue_conflicted
                else f"green and waiting {age} min"
            )
            + ("" if age <= limit else f", over the {limit:g} min limit"),
        )
        for key, age in oldest
    ]
    return ModelLabSourceRead(
        id=source.id,
        status="read",
        read_at=watcher.last_tick,
        summary=(
            (
                f"oldest in-scope green pull request has had conflicts for {oldest[0][1]} min"
                if oldest[0][0] in watcher.queue_conflicted
                else f"oldest in-scope green pull request waits {oldest[0][1]} min"
            )
            if oldest
            else "no in-scope green pull request is waiting"
        ),
        subjects=tuple(subjects),
    )


def read_deploy_lag(
    record: Mapping[str, Any] | None, now: datetime, overlay: ModelLabStateOverlay
) -> ModelLabSourceRead:
    source = _by_kind(overlay, "deploy_lag")
    merged = (record or {}).get("floor", {}).get("merged_running") if record else None
    if not isinstance(merged, dict):
        return _unread(
            source.id, "the merged-and-running count is not in the floor record"
        )
    try:
        read_at = parse_time(merged["generated_at"])
    except (KeyError, ValueError):
        return _unread(source.id, "the merged-and-running count has no time")
    cutoff = now - timedelta(days=overlay.deploy_lag_lookback_days)
    lags: list[tuple[str, int]] = []
    for day in merged.get("days", []):
        for item in day.get("merges", []):
            if item.get("running") or item.get("reason") != "not_yet_deployed":
                continue
            try:
                when = parse_time(item.get("merged_at"))
            except ValueError:
                continue
            if when >= cutoff:
                lags.append(
                    (
                        f"{item.get('repo')}#{item.get('number')}",
                        int((now - when).total_seconds() // 60),
                    )
                )
    lags.sort(key=lambda x: -x[1])
    limit = source.limit if source.limit is not None else 0.0
    # A merge reads "not running" only for want of evidence; when the count could not read the running build, say so on
    # the row, so a red that means "not placed" is not read as "not deployed".
    unread = [str(u) for u in merged.get("evidence_unread") or [] if str(u)]
    unread_note = f"; evidence unread: {unread[0][:110]}" if unread else ""
    subjects = [
        ModelLabSubject(
            name=key,
            ok=lag <= limit,
            value=float(lag),
            note=f"merged, not running, {lag} min{'' if lag <= limit else f', over the {limit:g} min limit'}{unread_note}",
        )
        for key, lag in lags[:3]
    ]
    return ModelLabSourceRead(
        id=source.id,
        status="read",
        read_at=read_at,
        summary=(
            f"oldest merge not yet running is {lags[0][1]} min old"
            if lags
            else "every recent merge is running"
        ),
        subjects=tuple(subjects),
    )


def _last_run(path: Path) -> tuple[datetime, dict[str, Any]]:
    """The finished time and the reports by condition of the last line of a run record."""
    with path.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(size - TAIL_BYTES, 0))
        lines = handle.read().splitlines()
    record = json.loads(lines[-1])
    return parse_time(record["finished_at"]), {
        r["condition"]: r for r in record["reports"]
    }


def read_broker_lag(
    path: Path, overlay: ModelLabStateOverlay, lag_path: Path, lag_condition: str
) -> ModelLabSourceRead:
    """The broker conditions from the lab alarm's last run, the lag condition from the lag node's last run.

    The alarm script's own lag condition watches a fixed list of consumer groups, one of which nobody has
    consumed since its writer was folded into the runtime; its lag grows with every delegation event and
    reads ALARM at every tick. The lag node derives the live groups from the running image, so its run
    replaces that one condition. Either record missing or unparseable is unread, never a smaller list, and
    the reading is as old as the older of the two runs.
    """
    source = _by_kind(overlay, "broker_lag")
    try:
        finished, reports = _last_run(path)
    except OSError as exc:
        return _unread(
            source.id,
            f"the lab alarm run record cannot be opened ({type(exc).__name__})",
        )
    except (ValueError, KeyError, IndexError, TypeError):
        return _unread(source.id, "the lab alarm's last run record cannot be parsed")
    try:
        lag_finished, lag_reports = _last_run(lag_path)
        if lag_condition not in lag_reports:
            raise KeyError(lag_condition)
    except OSError as exc:
        return _unread(
            source.id,
            f"the lag node's run record cannot be opened ({type(exc).__name__})",
        )
    except (ValueError, KeyError, IndexError, TypeError):
        return _unread(source.id, "the lag node's last run record cannot be parsed")
    reports[lag_condition] = lag_reports[lag_condition]
    finished = min(finished, lag_finished)
    subjects: list[ModelLabSubject] = []
    for condition in overlay.broker_conditions:
        report = reports.get(condition)
        if report is None:
            subjects.append(
                ModelLabSubject(
                    name=condition, ok=False, note="not in the lab alarm's last run"
                )
            )
            continue
        outcome = str(report.get("outcome", ""))
        subjects.append(
            ModelLabSubject(
                name=condition,
                ok=outcome == "OK",
                note=outcome
                if outcome == "OK"
                else f"{outcome}: {str(report.get('evidence', ''))[:110]}",
            )
        )
    return ModelLabSourceRead(
        id=source.id,
        status="read",
        read_at=finished,
        summary=f"{sum(1 for s in subjects if s.ok)} of {len(subjects)} broker conditions OK",
        subjects=tuple(subjects),
    )


def read_overlay_store(
    path: Path, overlay: ModelLabStateOverlay, now: datetime
) -> ModelLabSourceRead:
    """One subject per host and overlay: fresh with an answering store is ok; anything else says why.

    A stale table is still read by placement, so this row is what makes it visible. A reading older than
    the source's bound means that host's reader stopped, whatever the reading said when it was written."""
    source = _by_kind(overlay, "overlay_store")

    def judged(key: str, reading: Mapping[str, Any]) -> ModelLabSubject:
        age_s = (now - parse_time(reading["read_at"])).total_seconds()
        stopped = age_s > source.max_age_s
        return ModelLabSubject(
            name=key,
            ok=not stopped
            and reading["state"] == "fresh"
            and not reading.get("store_error"),
            value=reading.get("age_s"),
            note=(
                f"no reading for {age_s:.0f}s, over the {source.max_age_s}s bound"
                if stopped
                else str(
                    reading.get("store_error")
                    or reading.get("detail")
                    or reading["state"]
                )[:110]
            ),
        )

    try:
        readings = json.loads(path.read_bytes())["readings"]
        subjects = tuple(
            judged(key, reading) for key, reading in sorted(readings.items())
        )
        read_at = max(parse_time(r["read_at"]) for r in readings.values())
    except OSError as exc:
        return _unread(
            source.id,
            f"the overlay store fleet record cannot be opened ({type(exc).__name__})",
        )
    except (ValueError, KeyError, TypeError):
        return _unread(source.id, "the overlay store fleet record cannot be parsed")
    return ModelLabSourceRead(
        id=source.id,
        status="read",
        read_at=read_at,
        summary=f"{sum(1 for s in subjects if s.ok)} of {len(subjects)} overlay reads fresh",
        subjects=subjects,
    )


OVERLAY_STORE_EMITTER = "node_overlay_store_read_effect:floor_read"


def overlay_store_events(
    read: ModelLabSourceRead, piece: int, now: datetime, correlation_id: str
) -> tuple[ModelNodeEmission, ...]:
    """The row's golden or error chain for one cycle, for the floor's one emission sink.

    Golden when every subject is ok; an error chain, typed, when the record is unread or a read is stale
    or storeless, which is what a stale table placement still reads looks like from here."""
    failing = [subject.name for subject in read.subjects if not subject.ok]
    start = ModelNodeEmission(
        node=OVERLAY_STORE_EMITTER,
        piece=piece,
        correlation_id=correlation_id,
        phase="start",
        emitted_at=now,
    )
    if read.status != "read":
        code, detail = "overlay_store_unread", read.reason
    elif failing:
        code, detail = "overlay_store_not_fresh", ", ".join(failing)
    else:
        return (
            start,
            start.model_copy(
                update={
                    "phase": "success",
                    "counters": {"reads_fresh": len(read.subjects)},
                }
            ),
        )
    return (
        start,
        start.model_copy(
            update={
                "phase": "failure",
                "failure_code": code,
                "failure_detail": detail[:300],
            }
        ),
    )
