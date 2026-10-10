# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Pure parsing, aggregation and rendering decisions of the ledger friction rollup."""

from __future__ import annotations

import contextlib
import difflib
import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

# Expressions copied verbatim from the ledger friction guard.
_ROW_PREFIX = r"^\s*(?:[-*]\s+)?"
_STAMP = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z"
FRICTION_ROW_PATTERN = re.compile(
    _ROW_PREFIX + _STAMP + r"\s*\|\s*FRICTION(?![A-Za-z-])\s*\|"
)
COST_DURATION_PATTERN = re.compile(
    r"(?<![\w-])~?\s*<?\s*\d+(?:\.\d+)?(?:\s*[-\u2013]\s*\d+(?:\.\d+)?)?\s*"
    r"(?:lane-)?(?:hours?|hrs?|minutes?|mins?)\b",
    re.IGNORECASE,
)

_ROW_PATTERN = re.compile(
    r"^\s*(?:[-*]\s+)?(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z)"
    r"\s*\|\s*([A-Z][A-Z-]*)\s*\|"
)
_FIELD_PATTERN = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_-]*)\s*=\s*(.*?)\s*$")
_COST_COLON_PATTERN = re.compile(r"^\s*(costs?)\s*:\s*(.*?)\s*$", re.IGNORECASE)
_TICKET_PATTERN = re.compile(r"\bOMN-\d+\b", re.IGNORECASE)
_TRAILING_PARENTHETICAL = re.compile(r"\s*\(([^()]*)\)\s*$")
_NORMALISE_DASHES = re.compile(r"-+")
_DURATION_PATTERN = re.compile(
    r"(?<![\w#-])(?:about\s+|roughly\s+)?[~<]?\s*"
    r"(\d+(?:\.\d+)?)(?:\s*[-\u2013]\s*(\d+(?:\.\d+)?))?\s*"
    r"((?:lane[- ]|orchestrator[- ])?"
    r"(?:minutes?|mins?|m|hours?|hrs?|h))\b",
    re.IGNORECASE,
)
_RERUN_PATTERN = re.compile(r"\b(?:rerun|reran|re-ran|re-run|reruns)\b", re.IGNORECASE)
_RUN_ID_PATTERN = re.compile(r"\b\d{11}\b")
_QUEUE_EJECTION_PATTERN = re.compile(
    r"(?:\b(?:ejected|evicted|dequeued|kicked)\b.{0,60}\bqueue\b"
    r"|\bqueue\b.{0,60}\b(?:ejected|evicted|dequeued|kicked)\b"
    r"|\bremoved from the (?:merge )?queue\b"
    r"|\bdropped from the (?:merge )?queue\b"
    r"|\bleft the merge queue\b)",
    re.IGNORECASE,
)
_DELEGATE_RUNS_PATTERN = re.compile(
    r"delegate runs ok\s*=\s*\d+\s+failed\s*=\s*(\d+)", re.IGNORECASE
)
_DELEGATE_EQUALS_PATTERN = re.compile(
    r"delegate\s*=\s*ok\s+\d+\s*,\s*failed\s+(\d+)", re.IGNORECASE
)
_FAILED_ZERO_PATTERN = re.compile(r"\bfailed\s*=?\s*0\b", re.IGNORECASE)
_DELEGATION_NEAR_FAILURE = re.compile(
    r"(?:\b(?:delegation|onex delegate)\b.{0,80}\b(?:failed|refused|skipped|timed out)\b"
    r"|\b(?:failed|refused|skipped|timed out)\b.{0,80}"
    r"\b(?:delegation|onex delegate)\b)",
    re.IGNORECASE,
)
_GENERIC_CLASSES = frozenset({"refusal", "process", "tooling", "ci", "other"})
# Rows with no class= are counted and costed, but they are not one class, so
# they are never flagged as a recurring class.
UNCLASSIFIED = "unclassified"
_SIGNAL_NAMES = (
    "correction-row",
    "lane-refused-or-halted",
    "ci-rerun-repeated",
    "merge-queue-ejection",
    "drain-blocked-repeat",
    "delegation-failed-or-skipped",
)


@dataclass(frozen=True)
class LedgerRow:
    """One parsed row with its source provenance."""

    ts: datetime
    row_type: str
    fields: dict[str, tuple[str, ...]]
    text: str
    source: str
    line: int

    def first(self, name: str) -> str:
        """Return the first value for a case-normalised field."""
        values = self.fields.get(name.lower(), ())
        return values[0] if values else ""

    def all(self, names: Iterable[str]) -> tuple[str, ...]:
        """Return all values for the selected fields."""
        return tuple(value for name in names for value in self.fields.get(name, ()))


@dataclass
class SourceSummary:
    """Parse counts for one input source."""

    path: str
    status: str = "read"
    rows_parsed: int = 0
    rows_in_window: int = 0
    friction_rows_in_window: int = 0
    malformed_lines: int = 0

    def as_json(self) -> dict[str, object]:
        """Return a stable JSON-ready representation."""
        return {
            "path": self.path,
            "status": self.status,
            "rows_parsed": self.rows_parsed,
            "rows_in_window": self.rows_in_window,
            "friction_rows_in_window": self.friction_rows_in_window,
            "malformed_lines": self.malformed_lines,
        }


@dataclass(frozen=True)
class FrictionObservation:
    """One FRICTION row after local parsing but before fuzzy merging."""

    row: LedgerRow
    raw_class: str
    effective_class: str
    alias_applied: bool
    restatement: bool
    occurrences: int
    cost_minutes: float | None
    cost_text: str
    cost_source: str
    lane: str
    source_lane: str
    owning_tickets: frozenset[str]
    umbrella_tickets: frozenset[str]
    related: frozenset[str]
    guards: frozenset[str]


@dataclass
class ClassAccumulator:
    """Mutable aggregate for one final canonical class."""

    name: str
    occurrences: int = 0
    rows: int = 0
    restatements: int = 0
    cost_minutes: float = 0.0
    unparseable_cost_rows: int = 0
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    lanes: set[str] = field(default_factory=set)
    source_lanes: set[str] = field(default_factory=set)
    tickets: set[str] = field(default_factory=set)
    umbrella_tickets: set[str] = field(default_factory=set)
    related: set[str] = field(default_factory=set)
    guards: set[str] = field(default_factory=set)
    row_refs: list[dict[str, object]] = field(default_factory=list)


def parse_iso8601(value: str) -> datetime:
    """Parse the accepted UTC date or timestamp forms."""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC)
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _stamp(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalise_class(value: str) -> str:
    normalised = re.sub(r"[_\s]+", "-", value.strip().lower())
    return _NORMALISE_DASHES.sub("-", normalised).strip("-")


def parse_cost_minutes(text: str) -> float | None:
    """Sum every duration in cost text, returning None when none is parseable."""
    total = 0.0
    found = False
    for match in _DURATION_PATTERN.finditer(text):
        low = float(match.group(1))
        high = float(match.group(2)) if match.group(2) is not None else low
        value = (low + high) / 2
        unit = match.group(3).lower().replace(" ", "-")
        if "hour" in unit or unit.endswith("hr") or unit.endswith("hrs") or unit == "h":
            value *= 60
        total += value
        found = True
    return total if found else None


def _fields(cells: Sequence[str]) -> dict[str, tuple[str, ...]]:
    gathered: dict[str, list[str]] = {}
    for cell in cells:
        match = _FIELD_PATTERN.match(cell)
        if match is None:
            match = _COST_COLON_PATTERN.match(cell)
        if match is None:
            continue
        name, value = match.groups()
        gathered.setdefault(name.lower(), []).append(value.strip())
    return {name: tuple(values) for name, values in gathered.items()}


def parse_markdown_rows(text: str, source: str) -> list[LedgerRow]:
    """Parse current markdown rows with one-based line provenance."""
    rows: list[LedgerRow] = []
    for line_number, line in enumerate(text.splitlines(), start=1):
        match = _ROW_PATTERN.match(line)
        if match is None:
            continue
        try:
            timestamp = parse_iso8601(match.group(1))
        except ValueError:
            continue
        cells = line.split("|")[2:]
        rows.append(
            LedgerRow(
                ts=timestamp,
                row_type=match.group(2),
                fields=_fields(cells),
                text=line.strip(),
                source=source,
                line=line_number,
            )
        )
    return rows


def _json_string(payload: dict[str, Any], *names: str) -> str:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        if isinstance(value, list):
            return ",".join(str(item) for item in value)
    return ""


def parse_jsonl_rows(text: str, source: str) -> tuple[list[LedgerRow], int]:
    """Parse JSON-lines ledger rows and count malformed lines without failing."""
    rows: list[LedgerRow] = []
    malformed = 0
    aliases = {
        "timestamp": ("ts", "timestamp"),
        "row_type": ("type", "row_type"),
        "text": ("text", "detail"),
    }
    field_names = (
        "lane",
        "source-lane",
        "class",
        "cost",
        "ticket",
        "tickets",
        "existing",
        "related",
        "guard",
        "actor",
        "reason",
        "suppressed_since_last_row",
        "from",
        "pr",
        "outcome",
        "state",
    )
    for line_number, raw_line in enumerate(text.splitlines(), start=1):
        if not raw_line.strip():
            continue
        try:
            decoded: object = json.loads(raw_line)
        except (json.JSONDecodeError, TypeError):
            malformed += 1
            continue
        if not isinstance(decoded, dict):
            malformed += 1
            continue
        payload: dict[str, Any] = decoded
        timestamp_text = _json_string(payload, *aliases["timestamp"])
        row_type = _json_string(payload, *aliases["row_type"]).strip().upper()
        if not timestamp_text or not re.fullmatch(r"[A-Z][A-Z-]*", row_type):
            malformed += 1
            continue
        try:
            timestamp = parse_iso8601(timestamp_text)
        except ValueError:
            malformed += 1
            continue
        values: dict[str, tuple[str, ...]] = {}
        for name in field_names:
            value = _json_string(payload, name)
            if value:
                values[name] = (value,)
        detail = _json_string(payload, *aliases["text"])
        cells = [f"{name}={items[0]}" for name, items in values.items()]
        if detail:
            cells.append(detail)
        canonical = f"{_stamp(timestamp)} | {row_type} | " + " | ".join(cells)
        rows.append(
            LedgerRow(timestamp, row_type, values, canonical, source, line_number)
        )
    return rows, malformed


def _is_friction(row: LedgerRow) -> bool:
    probe = f"{_stamp(row.ts)} | {row.row_type} |"
    return FRICTION_ROW_PATTERN.match(probe) is not None


def _ticket_ids(values: Iterable[str]) -> set[str]:
    return {
        ticket.upper() for value in values for ticket in _TICKET_PATTERN.findall(value)
    }


def _cost(row: LedgerRow) -> tuple[str, str, float | None]:
    values = row.all(("cost", "costs"))
    if values:
        text = " ".join(values)
        return text, "cell", parse_cost_minutes(text)
    if row.first("actor").lower() == "hook":
        # A hook-recorded refusal carries no cost. Its detail text quotes the
        # refused command and the guard's message, whose durations ("the
        # 10-minute ceiling") are not what the refusal cost anyone.
        return "", "hook-no-cost", None
    inline = COST_DURATION_PATTERN.search(row.text)
    if inline is not None:
        text = inline.group(0)
        return text, "inline", parse_cost_minutes(text)
    return "", "missing", None


def _parse_aliases(values: Sequence[str]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"alias must be raw=canonical: {value}")
        raw, canonical = value.split("=", 1)
        raw_name = _normalise_class(raw)
        canonical_name = _normalise_class(canonical)
        if not raw_name or not canonical_name:
            raise ValueError(f"alias must name both classes: {value}")
        aliases[raw_name] = canonical_name
    return aliases


def _observation(
    row: LedgerRow, aliases: dict[str, str], umbrella_tickets: frozenset[str]
) -> FrictionObservation:
    raw_value = row.first("class").strip()
    raw_class = raw_value or "unclassified"
    parenthetical = _TRAILING_PARENTHETICAL.search(raw_class)
    restatement = bool(
        parenthetical is not None and "same instance" in parenthetical.group(1).lower()
    )
    class_without_note = _TRAILING_PARENTHETICAL.sub("", raw_class).strip()
    base = _normalise_class(class_without_note) or "unclassified"
    alias_applied = base in aliases
    effective = aliases.get(base, base)
    if not alias_applied and base in _GENERIC_CLASSES:
        reason = _normalise_class(row.first("reason"))
        if reason:
            if base == "refusal" and row.first("actor").lower() == "hook":
                effective = f"hook-refusal:{reason}"
            else:
                effective = f"{base}:{reason}"
    cost_text, cost_source, cost_minutes = _cost(row)
    tickets = _ticket_ids(row.all(("ticket", "tickets", "existing")))
    umbrellas = tickets & umbrella_tickets
    owning = tickets - umbrella_tickets
    related = _ticket_ids(row.all(("related",)))
    guard = row.first("guard")
    occurrences = 1
    suppressed = row.first("suppressed_since_last_row")
    if suppressed:
        with contextlib.suppress(ValueError):
            occurrences += max(0, int(suppressed))
    return FrictionObservation(
        row=row,
        raw_class=raw_class,
        effective_class=effective,
        alias_applied=alias_applied,
        restatement=restatement,
        occurrences=occurrences,
        cost_minutes=cost_minutes,
        cost_text=cost_text,
        cost_source=cost_source,
        lane=row.first("lane"),
        source_lane=row.first("source-lane"),
        owning_tickets=frozenset(owning),
        umbrella_tickets=frozenset(umbrellas),
        related=frozenset(related),
        guards=frozenset({guard} if guard and guard.lower() != "none" else set()),
    )


def _canonical_classes(observations: Sequence[FrictionObservation]) -> dict[str, str]:
    names = sorted({observation.effective_class for observation in observations})
    weights = {
        name: sum(
            observation.occurrences
            for observation in observations
            if observation.effective_class == name and not observation.restatement
        )
        for name in names
    }
    parents = {name: name for name in names}

    def find(name: str) -> str:
        while parents[name] != name:
            parents[name] = parents[parents[name]]
            name = parents[name]
        return name

    def union(left: str, right: str) -> None:
        left_root = find(left)
        right_root = find(right)
        if left_root != right_root:
            parents[max(left_root, right_root)] = min(left_root, right_root)

    for index, left in enumerate(names):
        for right in names[index + 1 :]:
            if difflib.SequenceMatcher(None, left, right).ratio() >= 0.9:
                union(left, right)
    groups: dict[str, list[str]] = {}
    for name in names:
        groups.setdefault(find(name), []).append(name)
    canonical: dict[str, str] = {}
    for members in groups.values():
        selected = min(members, key=lambda name: (-weights[name], name))
        canonical.update(dict.fromkeys(members, selected))
    return canonical


def _row_ref(observation: FrictionObservation) -> dict[str, object]:
    return {
        "ts": _stamp(observation.row.ts),
        "lane": observation.lane,
        "source_lane": observation.source_lane,
        "source": observation.row.source,
        "line": observation.row.line,
        "cost_source": observation.cost_source,
    }


def _class_json(accumulator: ClassAccumulator) -> dict[str, object]:
    assert accumulator.first_seen is not None
    assert accumulator.last_seen is not None
    return {
        "class": accumulator.name,
        "occurrences": accumulator.occurrences,
        "rows": accumulator.rows,
        "restatements": accumulator.restatements,
        "cost_minutes": round(accumulator.cost_minutes, 6),
        "unparseable_cost_rows": accumulator.unparseable_cost_rows,
        "first_seen": _stamp(accumulator.first_seen),
        "last_seen": _stamp(accumulator.last_seen),
        "lanes": sorted(accumulator.lanes),
        "source_lanes": sorted(accumulator.source_lanes),
        "tickets": sorted(accumulator.tickets),
        "umbrella_tickets_cited": sorted(accumulator.umbrella_tickets),
        "related": sorted(accumulator.related),
        "guards": sorted(accumulator.guards),
        "recurring_without_ticket": (
            accumulator.name != UNCLASSIFIED
            and accumulator.occurrences >= 2
            and not accumulator.tickets
        ),
        "recurring": accumulator.name != UNCLASSIFIED and accumulator.occurrences >= 2,
        "row_refs": sorted(
            accumulator.row_refs,
            key=lambda item: (str(item["ts"]), str(item["lane"]), str(item["source"])),
        ),
    }


def _signal_item(
    row: LedgerRow, key: str, recorded_lanes: set[str]
) -> dict[str, object]:
    lane = row.first("lane") or row.first("from")
    return {
        "ts": _stamp(row.ts),
        "lane": lane,
        "type": row.row_type,
        "source": row.source,
        "line": row.line,
        "key": key,
        "recorded": lane in recorded_lanes,
    }


def _grouped_signal_items(
    groups: dict[str, list[LedgerRow]], recorded_lanes: set[str]
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for key, grouped_rows in sorted(groups.items()):
        distinct = {(row.source, row.line): row for row in grouped_rows}
        if len(distinct) < 2:
            continue
        rows = sorted(distinct.values(), key=lambda row: (row.ts, row.source, row.line))
        item = _signal_item(rows[0], key, recorded_lanes)
        item["count"] = len(rows)
        item["rows"] = [
            {
                "ts": _stamp(row.ts),
                "lane": row.first("lane") or row.first("from"),
                "source": row.source,
                "line": row.line,
            }
            for row in rows
        ]
        items.append(item)
    return items


def _pr_key(row: LedgerRow) -> str:
    """The first token of pr=, since live cells carry extras (``pr=x#1 head=abc``)."""
    value = row.first("pr").split()
    return value[0].rstrip(",;") if value else ""


def _delegation_key(text: str) -> str | None:
    for pattern in (_DELEGATE_RUNS_PATTERN, _DELEGATE_EQUALS_PATTERN):
        match = pattern.search(text)
        if match is not None and int(match.group(1)) > 0:
            return "failed"
    if re.search(r"\bdelegated\s*=\s*0\b", text, re.IGNORECASE):
        return "skipped"
    if re.search(r"\bdelegate-skill-failed\b", text, re.IGNORECASE):
        return "failed"
    without_zero = _FAILED_ZERO_PATTERN.sub("", text)
    match = _DELEGATION_NEAR_FAILURE.search(without_zero)
    if match is None:
        return None
    return "skipped" if "skipped" in match.group(0).lower() else "failed"


def _signals(rows: Sequence[LedgerRow]) -> dict[str, dict[str, object]]:
    recorded_lanes: set[str] = set()
    for row in rows:
        if _is_friction(row):
            recorded_lanes.update(
                lane for lane in (row.first("lane"), row.first("source-lane")) if lane
            )

    items: dict[str, list[dict[str, object]]] = {name: [] for name in _SIGNAL_NAMES}
    reruns: dict[str, list[LedgerRow]] = {}
    drain_blocks: dict[str, list[LedgerRow]] = {}
    halt_prefixes = (
        "refused",
        "refuse",
        "halted",
        "halt",
        "declined",
        "aborted",
        "stopped",
        "report_only",
        "report-only",
        "yielded",
    )
    for row in rows:
        if row.row_type == "CORRECTION" or row.row_type.endswith("-CORRECTION"):
            items["correction-row"].append(
                _signal_item(row, row.row_type, recorded_lanes)
            )
        outcome = row.first("outcome")
        if row.row_type == "STATUS" and not outcome:
            outcome = row.first("state")
        lowered_outcome = outcome.lower().strip()
        if row.row_type in {"TERMINAL", "STATUS"}:
            prefix = next(
                (
                    candidate
                    for candidate in halt_prefixes
                    if lowered_outcome.startswith(candidate)
                ),
                None,
            )
            if prefix is not None:
                items["lane-refused-or-halted"].append(
                    _signal_item(row, prefix, recorded_lanes)
                )
        if _RERUN_PATTERN.search(row.text):
            pr = _pr_key(row)
            keys = [pr] if pr else _RUN_ID_PATTERN.findall(row.text)
            for key in keys:
                reruns.setdefault(key, []).append(row)
        if _QUEUE_EJECTION_PATTERN.search(row.text):
            match = _QUEUE_EJECTION_PATTERN.search(row.text)
            assert match is not None
            items["merge-queue-ejection"].append(
                _signal_item(
                    row, " ".join(match.group(0).lower().split()), recorded_lanes
                )
            )
        lane = row.first("lane")
        pr = _pr_key(row)
        if (
            row.row_type == "TERMINAL"
            and (lane.startswith("repo-drain") or "drain" in lane)
            and lowered_outcome.startswith("blocked")
            and pr
        ):
            drain_blocks.setdefault(pr, []).append(row)
        delegation_key = _delegation_key(row.text)
        if delegation_key is not None:
            items["delegation-failed-or-skipped"].append(
                _signal_item(row, delegation_key, recorded_lanes)
            )
    items["ci-rerun-repeated"] = _grouped_signal_items(reruns, recorded_lanes)
    items["drain-blocked-repeat"] = _grouped_signal_items(drain_blocks, recorded_lanes)

    result: dict[str, dict[str, object]] = {}
    for name in _SIGNAL_NAMES:
        selected = sorted(items[name], key=_signal_sort_key)
        result[name] = {
            "items": selected,
            "count": len(selected),
            "unrecorded_count": sum(not bool(item["recorded"]) for item in selected),
        }
    return result


def _signal_sort_key(item: dict[str, object]) -> tuple[str, str, int]:
    line = item["line"]
    assert isinstance(line, int)
    return str(item["ts"]), str(item["key"]), line


def _bucket_start(value: datetime, bucket_hours: int) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    bucket_seconds = bucket_hours * 3600
    elapsed = int((value - epoch).total_seconds())
    return epoch + timedelta(seconds=(elapsed // bucket_seconds) * bucket_seconds)


def _trend(
    observations: Sequence[FrictionObservation],
    canonical: dict[str, str],
    since: datetime,
    until: datetime,
    bucket_hours: int,
) -> list[dict[str, object]]:
    buckets: list[dict[str, object]] = []
    start = _bucket_start(since, bucket_hours)
    width = timedelta(hours=bucket_hours)
    while start < until:
        end = start + width
        selected = [
            observation
            for observation in observations
            if start <= observation.row.ts < end
        ]
        occurrences: dict[str, int] = {}
        cost = 0.0
        for observation in selected:
            if observation.restatement:
                continue
            name = canonical[observation.effective_class]
            occurrences[name] = occurrences.get(name, 0) + observation.occurrences
            cost += observation.cost_minutes or 0.0
        top = sorted(occurrences.items(), key=lambda item: (-item[1], item[0]))[:3]
        buckets.append(
            {
                "start": _stamp(start),
                "end": _stamp(end),
                "rows": len(selected),
                "occurrences": sum(occurrences.values()),
                "cost_minutes": round(cost, 6),
                "top_classes": [
                    {"class": name, "occurrences": count} for name, count in top
                ],
            }
        )
        start = end
    return buckets


def build_report(
    rows: Sequence[LedgerRow],
    sources: Sequence[SourceSummary],
    *,
    since: datetime,
    until: datetime,
    generated_at: datetime,
    bucket_hours: int,
    umbrella_tickets: frozenset[str],
    aliases: dict[str, str],
    duplicates_dropped: int,
    jsonl_provided: bool,
    jsonl_malformed_lines: int,
) -> dict[str, object]:
    """Build the stable JSON-ready report from deduplicated in-window rows."""
    observations = [
        _observation(row, aliases, umbrella_tickets)
        for row in rows
        if _is_friction(row)
    ]
    canonical = _canonical_classes(observations)
    accumulators: dict[str, ClassAccumulator] = {}
    unparseable: list[dict[str, object]] = []
    restatements: list[dict[str, object]] = []
    class_mapping: dict[str, dict[str, str]] = {}
    rows_with_class = 0
    for observation in observations:
        name = canonical[observation.effective_class]
        accumulator = accumulators.setdefault(name, ClassAccumulator(name=name))
        accumulator.rows += 1
        if observation.lane:
            accumulator.lanes.add(observation.lane)
        if observation.source_lane:
            accumulator.source_lanes.add(observation.source_lane)
        accumulator.tickets.update(observation.owning_tickets)
        accumulator.umbrella_tickets.update(observation.umbrella_tickets)
        accumulator.related.update(observation.related)
        accumulator.guards.update(observation.guards)
        accumulator.row_refs.append(_row_ref(observation))
        rows_with_class += int(bool(observation.row.first("class")))
        if observation.restatement:
            accumulator.restatements += 1
            restatements.append(
                {
                    "ts": _stamp(observation.row.ts),
                    "lane": observation.lane,
                    "class": name,
                    "raw_class": observation.raw_class,
                    "source": observation.row.source,
                    "line": observation.row.line,
                }
            )
        else:
            accumulator.occurrences += observation.occurrences
            accumulator.first_seen = min(
                observation.row.ts, accumulator.first_seen or observation.row.ts
            )
            accumulator.last_seen = max(
                observation.row.ts, accumulator.last_seen or observation.row.ts
            )
            if observation.cost_minutes is None:
                accumulator.unparseable_cost_rows += 1
                unparseable.append(
                    {
                        "ts": _stamp(observation.row.ts),
                        "lane": observation.lane,
                        "class": name,
                        "cost": observation.cost_text[:200],
                        "cost_source": observation.cost_source,
                        "source": observation.row.source,
                        "line": observation.row.line,
                    }
                )
            else:
                accumulator.cost_minutes += observation.cost_minutes

        mapping_key = observation.raw_class
        existing = class_mapping.get(mapping_key)
        mapping_value = {
            "canonical": name,
            "how": (
                "alias"
                if observation.alias_applied
                else "fuzzy"
                if observation.effective_class != name
                else "identity"
            ),
        }
        if existing is not None and existing != mapping_value:
            mapping_key = f"{mapping_key} [effective={observation.effective_class}]"
        class_mapping[mapping_key] = mapping_value

    for observation in observations:
        accumulator = accumulators[canonical[observation.effective_class]]
        if accumulator.first_seen is None:
            accumulator.first_seen = observation.row.ts
            accumulator.last_seen = observation.row.ts
    classes = [_class_json(accumulator) for accumulator in accumulators.values()]
    classes.sort(key=_class_sort_key)
    recurring_without_ticket = sorted(
        str(item["class"]) for item in classes if item["recurring_without_ticket"]
    )
    recurring = sorted(str(item["class"]) for item in classes if item["recurring"])
    occurrences_total = sum(
        accumulator.occurrences for accumulator in accumulators.values()
    )
    cost_total = sum(accumulator.cost_minutes for accumulator in accumulators.values())
    return {
        "window": {"since": _stamp(since), "until": _stamp(until)},
        "generated_at": _stamp(generated_at),
        "sources": [source.as_json() for source in sources],
        "duplicates_dropped": duplicates_dropped,
        "jsonl": {
            "status": "provided" if jsonl_provided else "not provided",
            "malformed_lines": jsonl_malformed_lines,
        },
        "totals": {
            "friction_rows": len(observations),
            "occurrences": occurrences_total,
            "restatements": sum(
                accumulator.restatements for accumulator in accumulators.values()
            ),
            "cost_minutes": round(cost_total, 6),
            "cost_hours": round(cost_total / 60, 2),
            "unparseable_cost_rows": len(unparseable),
            "rows_with_class": rows_with_class,
            "rows_without_class": len(observations) - rows_with_class,
        },
        "classes": classes,
        "flags": {
            "recurring_without_ticket": recurring_without_ticket,
            "recurring": recurring,
        },
        "class_mapping": {key: class_mapping[key] for key in sorted(class_mapping)},
        "unparseable_costs": sorted(
            unparseable, key=lambda item: (str(item["ts"]), str(item["lane"]))
        ),
        "restatements": sorted(
            restatements, key=lambda item: (str(item["ts"]), str(item["lane"]))
        ),
        "largest_costs": _largest_costs(observations, canonical),
        "trend": _trend(observations, canonical, since, until, bucket_hours),
        "signals": _signals(rows),
    }


def _largest_costs(
    observations: Sequence[FrictionObservation],
    canonical: dict[str, str],
    limit: int = 5,
) -> list[dict[str, object]]:
    """The rows that dominate the cost sum, so an outlier is visible, not averaged in.

    A lane writes its own cost, and some write elapsed time (PR age, wall-clock
    waiting) as lane-hours; the parser cannot tell, so the report shows the rows.
    """
    costed = [
        observation
        for observation in observations
        if not observation.restatement and observation.cost_minutes
    ]
    costed.sort(
        key=lambda item: (-(item.cost_minutes or 0.0), item.row.ts, item.row.line)
    )
    return [
        {
            "ts": _stamp(observation.row.ts),
            "lane": observation.lane,
            "class": canonical[observation.effective_class],
            "cost_minutes": round(observation.cost_minutes or 0.0, 6),
            "cost": observation.cost_text[:200],
            "source": observation.row.source,
            "line": observation.row.line,
        }
        for observation in costed[:limit]
    ]


def _class_sort_key(item: dict[str, object]) -> tuple[float, int, str]:
    cost = item["cost_minutes"]
    occurrences = item["occurrences"]
    assert isinstance(cost, (int, float))
    assert not isinstance(cost, bool)
    assert isinstance(occurrences, int)
    assert not isinstance(occurrences, bool)
    return -float(cost), -occurrences, str(item["class"])


def _format_number(value: object) -> str:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"expected a number, got {type(value).__name__}")
    number = float(value)
    return (
        str(int(number))
        if number.is_integer()
        else f"{number:.2f}".rstrip("0").rstrip(".")
    )


def render_markdown(report: dict[str, object]) -> str:
    """Render the JSON-ready report as a compact operator-facing markdown report."""
    window = report["window"]
    totals = report["totals"]
    flags = report["flags"]
    jsonl = report["jsonl"]
    assert isinstance(window, dict)
    assert isinstance(totals, dict)
    assert isinstance(flags, dict)
    assert isinstance(jsonl, dict)
    lines = [
        f"# Friction rollup — {window['since']} to {window['until']}",
        "",
        "Unrecorded-friction signal detectors are heuristics over free text.",
        "",
        (
            f"Totals: {totals['friction_rows']} FRICTION rows; "
            f"{totals['occurrences']} occurrences; "
            f"{_format_number(totals['cost_minutes'])} cost minutes "
            f"({totals['cost_hours']} hours); {totals['restatements']} restatements; "
            f"{totals['unparseable_cost_rows']} unparseable costs; "
            f"class= present {totals['rows_with_class']}, absent {totals['rows_without_class']}; "
            f"duplicates dropped {report['duplicates_dropped']}."
        ),
        "",
    ]
    source_bits: list[str] = []
    sources = report["sources"]
    assert isinstance(sources, list)
    for source in sources:
        assert isinstance(source, dict)
        if source["status"] == "missing":
            source_bits.append(f"{source['path']}: missing")
        else:
            source_bits.append(
                f"{source['path']}: {source['rows_in_window']} rows in window"
            )
    lines.extend(
        [
            "Sources: " + "; ".join(source_bits) + ".",
            f"jsonl: {jsonl['status']}",
            "",
            "## Recurring classes with no owning ticket",
            "",
        ]
    )
    flagged = flags["recurring_without_ticket"]
    assert isinstance(flagged, list)
    if flagged:
        lines.extend(f"- {name}" for name in flagged)
    else:
        lines.append("none")
    lines.extend(
        [
            "",
            "## Classes",
            "",
            "| class | occurrences | cost (min) | first | last | lanes | tickets |",
            "| -- | --: | --: | -- | -- | -- | -- |",
        ]
    )
    classes = report["classes"]
    assert isinstance(classes, list)
    for item in classes:
        assert isinstance(item, dict)
        lines.append(
            f"| {item['class']} | {item['occurrences']} | "
            f"{_format_number(item['cost_minutes'])} | {item['first_seen']} | "
            f"{item['last_seen']} | {', '.join(item['lanes']) or '-'} | "
            f"{', '.join(item['tickets']) or '-'} |"
        )
    lines.extend(["", "## Largest single-row costs", ""])
    largest = report["largest_costs"]
    assert isinstance(largest, list)
    if not largest:
        lines.append("none")
    for item in largest:
        assert isinstance(item, dict)
        lines.append(
            f"- {_format_number(item['cost_minutes'])} min: {item['ts']} lane={item['lane']} "
            f"— {item['class']}: `{item['cost']}` ({item['source']}:{item['line']})"
        )
    lines.extend(["", "## Unparseable costs", ""])
    unparseable = report["unparseable_costs"]
    assert isinstance(unparseable, list)
    hook_rows: dict[str, int] = {}
    lane_rows: list[dict[str, object]] = []
    for item in unparseable:
        assert isinstance(item, dict)
        if item["cost_source"] == "hook-no-cost":
            name = str(item["class"])
            hook_rows[name] = hook_rows.get(name, 0) + 1
        else:
            lane_rows.append(item)
    if not unparseable:
        lines.append("none")
    if hook_rows:
        lines.append(
            f"- {sum(hook_rows.values())} hook-recorded refusal rows carry no cost: "
            + ", ".join(
                f"{name} ({count})" for name, count in sorted(hook_rows.items())
            )
            + ". Every row is in the JSON output."
        )
    for item in lane_rows:
        raw = str(item["cost"]) or "(missing)"
        lines.append(
            f"- {item['ts']} lane={item['lane']} — {item['class']}: `{raw}` "
            f"({item['source']}:{item['line']})"
        )
    lines.extend(["", "## Restatements", ""])
    restatements = report["restatements"]
    assert isinstance(restatements, list)
    if not restatements:
        lines.append("none")
    for item in restatements:
        assert isinstance(item, dict)
        lines.append(
            f"- {item['ts']} lane={item['lane']} — {item['class']} "
            f"({item['source']}:{item['line']})"
        )
    lines.extend(["", "## Class mapping", ""])
    mapping = report["class_mapping"]
    assert isinstance(mapping, dict)
    identity_count = sum(
        isinstance(value, dict) and value.get("how") == "identity"
        for value in mapping.values()
    )
    non_identity = [
        (raw, value)
        for raw, value in mapping.items()
        if isinstance(value, dict) and value.get("how") != "identity"
    ]
    lines.append(f"Identity mappings: {identity_count}.")
    if not non_identity:
        lines.append("Non-identity mappings: none.")
    else:
        for raw, value in non_identity:
            lines.append(f"- `{raw}` → `{value['canonical']}` ({value['how']})")
    lines.extend(
        [
            "",
            "## Trend",
            "",
            "| bucket | rows | occurrences | cost (min) | top classes |",
            "| -- | --: | --: | --: | -- |",
        ]
    )
    trend = report["trend"]
    assert isinstance(trend, list)
    for bucket in trend:
        assert isinstance(bucket, dict)
        top_classes = bucket["top_classes"]
        assert isinstance(top_classes, list)
        top = ", ".join(
            f"{item['class']} ({item['occurrences']})"
            for item in top_classes
            if isinstance(item, dict)
        )
        lines.append(
            f"| {bucket['start']} - {bucket['end']} | {bucket['rows']} | "
            f"{bucket['occurrences']} | {_format_number(bucket['cost_minutes'])} | {top or '-'} |"
        )
    lines.extend(
        [
            "",
            "## Unrecorded friction signals",
            "",
            "| signal | items | unrecorded |",
            "| -- | --: | --: |",
        ]
    )
    signals = report["signals"]
    assert isinstance(signals, dict)
    for name in _SIGNAL_NAMES:
        signal = signals[name]
        assert isinstance(signal, dict)
        lines.append(f"| {name} | {signal['count']} | {signal['unrecorded_count']} |")
    for name in _SIGNAL_NAMES:
        signal = signals[name]
        assert isinstance(signal, dict)
        examples = signal["items"]
        assert isinstance(examples, list)
        if not examples:
            continue
        lines.extend(["", f"### {name} examples", ""])
        for item in examples[:10]:
            assert isinstance(item, dict)
            count = f"; count={item['count']}" if "count" in item else ""
            lines.append(
                f"- {item['ts']} lane={item['lane']} — key={item['key']}; "
                f"recorded={str(item['recorded']).lower()}{count} "
                f"({item['source']}:{item['line']})"
            )
    row_references: list[tuple[str, str, str, str, object]] = []
    for item in classes:
        assert isinstance(item, dict)
        refs = item["row_refs"]
        assert isinstance(refs, list)
        for ref in refs:
            assert isinstance(ref, dict)
            row_references.append(
                (
                    str(ref["ts"]),
                    str(ref["lane"]),
                    str(item["class"]),
                    str(ref["source"]),
                    ref["line"],
                )
            )
    lines.extend(["", "## FRICTION row references", ""])
    for timestamp, lane, class_name, source, line_number in sorted(row_references):
        lines.append(
            f"- {timestamp} lane={lane} — {class_name} ({source}:{line_number})"
        )
    return "\n".join(lines) + "\n"
