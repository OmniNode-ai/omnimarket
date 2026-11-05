# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Readers of the sources the 17a floor draws from (OMN-19998).

Each reader returns what was read, with the time of the read, or says it could not read: it never returns a zero or an
empty value for a source it could not read. Paths are the contract's; limits and class names are the overlay's.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from omnimarket.models.floor_17a import (
    ModelFloorCauseIn,
    ModelFloorLane,
    ModelGoalScope,
    ModelLabSourceDef,
    ModelLabSourceRead,
    ModelLabStateOverlay,
    ModelLabSubject,
)

TICKET = re.compile(r"\bOMN-\d+\b")
SIZE_UNITS = {"K": 1 / 1024 / 1024, "M": 1 / 1024, "G": 1.0, "T": 1024.0}
TAIL_BYTES = 400_000


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
        )
        for item in lanes
        if isinstance(item, dict) and item.get("lane")
    )


def record_stoppages(record: Mapping[str, Any]) -> tuple[ModelFloorCauseIn, ...]:
    out: list[ModelFloorCauseIn] = []
    for item in record["floor"].get("stoppages", []):
        if not isinstance(item, dict) or not item.get("name"):
            continue
        name = str(item["name"])
        out.append(
            ModelFloorCauseIn(
                id=f"stoppage:{name}",
                title=name.replace("-", " ").replace(":", ": "),
                detail=(
                    f"{item.get('occurrences', 0)} times, {item.get('cost_minutes', 0):g} lane-minutes lost, "
                    f"last {str(item.get('last_seen', ''))[:16]}."
                ),
                size=int(item.get("occurrences", 0)),
                size_unit="occurrences",
                tickets=tuple(str(t) for t in item.get("tickets", [])),
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
    paused_open: int | None = None
    class_counts: dict[str, int] = field(default_factory=dict)
    class_tickets: dict[str, tuple[str, ...]] = field(default_factory=dict)
    queue_ages: tuple[tuple[str, int], ...] = ()


def read_watcher(
    path: Path,
    now: datetime,
    *,
    excluded_classes: tuple[str, ...],
    queue_classes: tuple[str, ...],
    merge_window_minutes: int,
    scope: ModelGoalScope | None,
) -> WatcherReading | None:
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
    open_prs = outside = paused = 0
    ages: list[tuple[str, int]] = []
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
        if cls in queue_classes and not facts.get("draft", False):
            done = [
                parse_time(d[2])
                for d in (value.get("ci") or {}).get("detail", [])
                if isinstance(d, list) and len(d) > 2 and d[2]
            ]
            if done:
                ages.append((key, max(int((now - max(done)).total_seconds() // 60), 0)))
    cutoff = now - timedelta(minutes=merge_window_minutes)
    recent = recent_outside = 0
    for key, value in merges.items():
        try:
            merged_at = parse_time(value.get("merged_at"))
        except ValueError:
            continue
        if merged_at < cutoff:
            continue
        if (
            scope.classify(
                str(key), str(value.get("title", "")), str(value.get("head_ref", ""))
            )
            == "in_scope"
        ):
            recent += 1
        else:
            recent_outside += 1
    return WatcherReading(
        last_tick=last_tick,
        scoped=True,
        open_prs=open_prs,
        merges_in_window=recent,
        open_outside=outside,
        merges_outside=recent_outside,
        paused_open=paused,
        class_counts=counts,
        class_tickets={k: tuple(dict.fromkeys(v)) for k, v in tickets.items()},
        queue_ages=tuple(sorted(ages, key=lambda a: -a[1])),
    )


# --- the lab sources ---------------------------------------------------------------------------------------------------


def _unread(source_id: str, reason: str) -> ModelLabSourceRead:
    return ModelLabSourceRead(id=source_id, status="unread", reason=reason)


def _by_kind(overlay: ModelLabStateOverlay, kind: str) -> ModelLabSourceDef:
    return next(s for s in overlay.sources if s.kind == kind)


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
        refused = admission.get("state") == "refused"
        subjects.append(
            ModelLabSubject(
                name=host,
                ok=not refused,
                note=str(admission.get("reason", ""))[:140]
                if refused
                else "advertising",
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


def read_runner_groups(path: Path, overlay: ModelLabStateOverlay) -> ModelLabSourceRead:
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
        rollup = data["class_rollup"]
        rows = data.get("runners", [])
    except (KeyError, ValueError, TypeError):
        return _unread(
            source.id, "the runner fleet file has no observed_at or class_rollup"
        )
    versions: dict[str, set[str]] = {}
    for row in rows:
        if isinstance(row, dict) and row.get("version"):
            versions.setdefault(str(row.get("label_class", "")), set()).add(
                str(row["version"])
            )
    limit = int(source.limit) if source.limit is not None else 0
    subjects: list[ModelLabSubject] = []
    for group in rollup:
        name = str(group.get("label_class", "?"))
        offline = int(group.get("offline", 0))
        seen = versions.get(name)
        version = ", ".join(sorted(seen)) if seen else "version not reported"
        ok = offline <= limit and not (overlay.require_runner_version and not seen)
        subjects.append(
            ModelLabSubject(
                name=name,
                ok=ok,
                value=float(group.get("online", 0)),
                note=f"{group.get('online', 0)} online, {offline} offline, {version}",
            )
        )
    return ModelLabSourceRead(
        id=source.id,
        status="read",
        read_at=observed,
        summary=f"{len(subjects)} runner groups read",
        subjects=tuple(subjects),
    )


def _free_gb(entry: str, key: str) -> float | None:
    match = re.search(rf"\b{re.escape(key)}=([\d.]+)([KMGT])/", entry)
    return float(match.group(1)) * SIZE_UNITS[match.group(2)] if match else None


def read_disk(path: Path, overlay: ModelLabStateOverlay) -> ModelLabSourceRead:
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
        free = [f for k in overlay.disk_keys if (f := _free_gb(rest, k)) is not None]
        if not free:
            subjects.append(
                ModelLabSubject(name=host, ok=False, note="no disk figure in the read")
            )
            continue
        low = min(free)
        subjects.append(
            ModelLabSubject(
                name=host,
                ok=low >= floor,
                value=round(low, 1),
                note=f"{low:.1f} GB free{'' if low >= floor else f', under the {floor:g} GB floor'}",
            )
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
            note=f"green and waiting {age} min{'' if age <= limit else f', over the {limit:g} min limit'}",
        )
        for key, age in oldest
    ]
    return ModelLabSourceRead(
        id=source.id,
        status="read",
        read_at=watcher.last_tick,
        summary=(
            f"oldest in-scope green pull request waits {oldest[0][1]} min"
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
    subjects = [
        ModelLabSubject(
            name=key,
            ok=lag <= limit,
            value=float(lag),
            note=f"merged, not running, {lag} min{'' if lag <= limit else f', over the {limit:g} min limit'}",
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


def read_broker_lag(path: Path, overlay: ModelLabStateOverlay) -> ModelLabSourceRead:
    source = _by_kind(overlay, "broker_lag")
    try:
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(size - TAIL_BYTES, 0))
            lines = handle.read().splitlines()
        record = json.loads(lines[-1])
        finished = parse_time(record["finished_at"])
        reports = {r["condition"]: r for r in record["reports"]}
    except OSError as exc:
        return _unread(
            source.id,
            f"the lab alarm run record cannot be opened ({type(exc).__name__})",
        )
    except (ValueError, KeyError, IndexError, TypeError):
        return _unread(source.id, "the lab alarm's last run record cannot be parsed")
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
