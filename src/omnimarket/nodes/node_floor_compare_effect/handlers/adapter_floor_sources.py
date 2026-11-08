# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The dated sources behind the floor's history, and the same counts for today (OMN-23018, AC1 backfill).

The work ledger and its split copies, the throughput tick log, the canonical clones' git history, the landing controller's
decisions file, the lab alarm history and the floor alarm history already hold dated readings. This module reads them into
a ``Book`` and turns the book into dated ``ModelSnapshot`` points, through one measure:

* a counted key is the number of records in the trailing ``window_hours`` that end at the point's time, so a point stored
  for a past hour and the value read for now are the same measure;
* a source with too few records in that window gives no value at all (the point leaves its keys out), so a day the source
  did not record stays unread and is never stored as zero;
* a level key (merges, open pull requests, parked causes, breached floors) is the sample the source logged at that time.

Nothing here invents a reading: every value is parsed from a record, and every point names its source.
"""

from __future__ import annotations

import bisect
import json
import os
import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml
from omnibase_core.validators.no_unguarded_git_subprocess import (
    scrub_git_location_env,
)

from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelHistorySources,
    ModelSnapshot,
)
from omnimarket.nodes.node_friction_rollup_compute.handlers import (
    friction_rollup_core as core,
)

SRC_LEDGER = "ledger"
SRC_GIT = "git"
SRC_LANDING = "landing-decisions"
SRC_LAB_ALARM = "lab-alarm"
SRC_TICK_LOG = "throughput-tick"
SRC_FLOOR_ALARM = "floor-alarm"
SRC_FRICTION = "friction-ledger"
BACKFILL_PREFIX = "backfill:"
FRICTION_PREFIXES = ("cause:stoppage:", "stoppage_cost:stoppage:")
_ARCHIVE_NAME = re.compile(r"_(\d{4}-\d{2}-\d{2})-split\.md$")
_WATCHER_TICK = re.compile(r"watcher tick (\S+?)\)")
_MERGES_NOTE = re.compile(r"^NOTE merges last 60 min: (\d+) \(")
_GOAL_LINE = re.compile(r"^GOAL changed=\S+ \| merges_60m=(\d+) open=(\d+)")
_FILL_SENT = re.compile(r"^NOTE lab-fill \| sent=(\d+)")
_TS_FIELD = re.compile(r'"ts": "([^"]+)"')
_CHUNK = 4 << 20
_COVER_SLACK_S = 3600.0


def _epoch(value: str) -> float | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.timestamp()


@dataclass
class Book:
    """Everything read from the dated sources, sorted, with the notes of what could not be read."""

    events: dict[str, list[float]] = field(default_factory=dict)
    keys_of: dict[str, list[str]] = field(default_factory=dict)
    alive: dict[str, list[float]] = field(default_factory=dict)
    samples: dict[str, list[tuple[float, dict[str, float]]]] = field(
        default_factory=dict
    )
    tick_sent: list[tuple[float, float]] = field(default_factory=list)
    friction_rows: list[core.LedgerRow] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def add_event(self, source: str, key: str, at: float) -> None:
        self.events.setdefault(key, []).append(at)
        keys = self.keys_of.setdefault(source, [])
        if key not in keys:
            keys.append(key)

    def declare(self, source: str, key: str) -> None:
        self.events.setdefault(key, [])
        keys = self.keys_of.setdefault(source, [])
        if key not in keys:
            keys.append(key)

    def finish(self) -> None:
        for times in self.events.values():
            times.sort()
        for times in self.alive.values():
            times.sort()
        for rows in self.samples.values():
            rows.sort(key=lambda item: item[0])
        self.tick_sent.sort()
        self.friction_rows.sort(key=lambda row: (row.ts, row.source, row.line))


def _count(times: list[float], low: float, high: float) -> int:
    return bisect.bisect_right(times, high) - bisect.bisect_right(times, low)


def _tail_lines(path: Path, done: Callable[[list[str]], bool]) -> list[str]:
    """The lines of ``path`` from its end backwards until ``done`` says the earliest complete lines are old enough."""
    size = path.stat().st_size
    position, data = size, b""
    with path.open("rb") as handle:
        while position > 0:
            step = min(_CHUNK, position)
            position -= step
            handle.seek(position)
            data = handle.read(step) + data
            lines = data.split(b"\n")
            body = lines[1:] if position > 0 else lines
            head = [raw.decode("utf-8", "replace") for raw in body[:300]]
            if position == 0 or done(head):
                return [raw.decode("utf-8", "replace") for raw in body]
    return []


def _ledger_files(main: Path, archive_dirname: str, since: datetime) -> list[Path]:
    """The live ledger and every split copy that can hold a row at or after ``since`` (a split named D ends on D)."""
    files: list[Path] = []
    archive = main.parent / archive_dirname
    if archive.is_dir():
        for path in sorted(archive.iterdir()):
            match = _ARCHIVE_NAME.search(path.name)
            if match and match.group(1) >= since.strftime("%Y-%m-%d"):
                files.append(path)
    files.append(main)
    return files


def load_ledger(
    book: Book, hs: ModelHistorySources, env: Mapping[str, str], since: datetime
) -> None:
    raw = env.get(hs.ledger_env, "").strip()
    if not raw:
        book.notes.append(f"{hs.ledger_env} is not set: the ledger counts are unread")
        return
    try:
        main = Path(raw).resolve(strict=True)
    except OSError as exc:
        book.notes.append(
            f"the ledger {raw} cannot be read ({exc.__class__.__name__}): its counts are unread"
        )
        return
    for key in (
        "lanes_started",
        *(f"lanes_ended:{o}" for o in hs.ledger_lane_outcomes),
    ):
        book.declare(SRC_LEDGER, key)
    seen: set[str] = set()
    since_ts = since.timestamp()
    wanted = set(hs.ledger_lane_outcomes)
    for path in _ledger_files(main, hs.ledger_archive_dirname, since):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            book.notes.append(f"{path.name} cannot be read ({exc.__class__.__name__})")
            continue
        for row in core.parse_markdown_rows(text, path.name):
            at = row.ts.timestamp()
            if at < since_ts or row.text in seen:
                continue
            seen.add(row.text)
            book.alive.setdefault(SRC_LEDGER, []).append(at)
            if row.row_type == "CLAIM":
                book.add_event(SRC_LEDGER, "lanes_started", at)
            elif row.row_type == "TERMINAL":
                outcome = row.first("outcome").strip().lower().rstrip(":")
                if outcome in wanted:
                    book.add_event(SRC_LEDGER, f"lanes_ended:{outcome}", at)
            elif row.row_type == "FRICTION":
                book.friction_rows.append(row)


def load_throughput(
    book: Book, hs: ModelHistorySources, home: Path, since: datetime
) -> None:
    path = home / hs.throughput_relpath
    since_ts = since.timestamp()

    def old_enough(head: list[str]) -> bool:
        for line in head:
            hit = (
                _WATCHER_TICK.search(line)
                if line.startswith("NOTE merges last 60 min")
                else None
            )
            stamp = _epoch(hit.group(1)) if hit else None
            if stamp is not None and stamp < since_ts:
                return True
        return False

    try:
        lines = _tail_lines(path, old_enough)
    except OSError as exc:
        book.notes.append(
            f"{hs.throughput_relpath} cannot be read ({exc.__class__.__name__}): the tick log numbers are unread"
        )
        return
    blocks: list[dict[str, float]] = []
    current: dict[str, float] | None = None
    for line in lines:
        if line.startswith("NOTE controller live"):
            current = {"parked": 0.0}
            blocks.append(current)
            continue
        if current is None:
            continue
        if line.startswith("NOTE merges last 60 min"):
            merges, stamp = _MERGES_NOTE.match(line), _WATCHER_TICK.search(line)
            at = _epoch(stamp.group(1)) if stamp else None
            if merges and at is not None:
                current["at"], current["merges_all"] = at, float(merges.group(1))
        elif line.startswith("GOAL changed"):
            goal = _GOAL_LINE.match(line)
            if goal:
                current["merges"], current["open_prs"] = (
                    float(goal.group(1)),
                    float(goal.group(2)),
                )
        elif line.startswith("NOTE lab-fill |"):
            fill = _FILL_SENT.match(line)
            if fill:
                current["sent"] = float(fill.group(1))
        elif "| parked cause" in line:
            current["parked"] += 1.0
    last = 0.0
    kept: list[tuple[float, dict[str, float]]] = []
    for block in blocks:
        at = block.get("at")
        if at is None or at < since_ts or at <= last:
            continue
        last = at
        values = {"merges_all": block["merges_all"], "parked_causes": block["parked"]}
        for key in ("merges", "open_prs"):
            if key in block:
                values[key] = block[key]
        kept.append((at, values))
        if "sent" in block:
            book.tick_sent.append((at, block["sent"]))
    book.samples[SRC_TICK_LOG] = kept


def load_floor_alarm(
    book: Book, hs: ModelHistorySources, home: Path, since: datetime
) -> None:
    path = home / hs.floor_alarm_runs_relpath
    since_ts = since.timestamp()

    def old_enough(head: list[str]) -> bool:
        for line in head:
            hit = re.search(r'"ts": "([^"]+)"', line)
            stamp = _epoch(hit.group(1)) if hit else None
            if stamp is not None and stamp < since_ts:
                return True
        return False

    try:
        lines = _tail_lines(path, old_enough)
    except OSError as exc:
        book.notes.append(
            f"{hs.floor_alarm_runs_relpath} cannot be read ({exc.__class__.__name__}): floor breaches are unread"
        )
        return
    rows: list[tuple[float, dict[str, float]]] = []
    for line in lines:
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        at = _epoch(str(doc.get("ts", ""))) if isinstance(doc, dict) else None
        breaches = doc.get("breaches") if isinstance(doc, dict) else None
        if at is None or at < since_ts or not isinstance(breaches, dict):
            continue
        rows.append(
            (
                at,
                {
                    "floor_breaches": float(
                        sum(1 for k in breaches if not str(k).startswith("unknown:"))
                    )
                },
            )
        )
    book.samples[SRC_FLOOR_ALARM] = rows


def load_landing(
    book: Book, hs: ModelHistorySources, env: Mapping[str, str], since: datetime
) -> None:
    value = env.get(hs.landing_env, "").strip()
    if not value:
        book.notes.append(f"{hs.landing_env} is not set: the landing counts are unread")
        return
    directory = Path(value)
    path = directory / hs.landing_decisions_file
    since_ts = since.timestamp()

    def old_enough(head: list[str]) -> bool:
        for line in head:
            hit = _TS_FIELD.search(line)
            stamp = _epoch(hit.group(1)) if hit else None
            if stamp is not None and stamp < since_ts:
                return True
        return False

    try:
        lines = _tail_lines(path, old_enough)
    except OSError as exc:
        book.notes.append(
            f"{path} cannot be read ({exc.__class__.__name__}): the landing counts are unread"
        )
        return
    for key in (
        "landing_merged",
        "landing_armed",
        "landing_merge_failed",
        "landing_refused",
    ):
        book.declare(SRC_LANDING, key)
    refusals = set(hs.landing_refusal_outcomes)
    for line in lines:
        try:
            doc = json.loads(line)
        except ValueError:
            continue
        if not isinstance(doc, dict):
            continue
        at = _epoch(str(doc.get("ts", "")))
        if at is None or at < since_ts:
            continue
        book.alive.setdefault(SRC_LANDING, []).append(at)
        kind, outcome = doc.get("kind"), doc.get("outcome")
        if kind == "merge" and outcome == "merged":
            book.add_event(SRC_LANDING, "landing_merged", at)
        elif kind == "merge" and outcome == "enqueued":
            book.add_event(SRC_LANDING, "landing_armed", at)
        elif kind == "merge" and outcome == "failed":
            book.add_event(SRC_LANDING, "landing_merge_failed", at)
        if outcome in refusals:
            book.add_event(SRC_LANDING, "landing_refused", at)


def load_lab_alarms(
    book: Book, hs: ModelHistorySources, home: Path, since: datetime
) -> None:
    since_ts = since.timestamp()
    runs, alarms = home / hs.lab_alarm_runs_relpath, home / hs.lab_alarms_relpath
    try:
        for line in runs.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            at = (
                _epoch(str(doc.get("finished_at", "")))
                if isinstance(doc, dict)
                else None
            )
            if at is not None and at >= since_ts:
                book.alive.setdefault(SRC_LAB_ALARM, []).append(at)
        book.declare(SRC_LAB_ALARM, "lab_alarms_total")
        for line in alarms.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                doc = json.loads(line)
            except ValueError:
                continue
            at = (
                _epoch(str(doc.get("raised_at", ""))) if isinstance(doc, dict) else None
            )
            if at is None or at < since_ts or not doc.get("condition"):
                continue
            book.add_event(SRC_LAB_ALARM, f"lab_alarms:{doc['condition']}", at)
            book.add_event(SRC_LAB_ALARM, "lab_alarms_total", at)
    except OSError as exc:
        book.notes.append(
            f"the lab alarm history cannot be read ({exc.__class__.__name__}): its counts are unread"
        )


def _git(args: list[str], timeout: int) -> str | None:
    try:
        done = subprocess.run(
            ["git", *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=scrub_git_location_env(os.environ),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout if done.returncode == 0 else None


def load_git(book: Book, hs: ModelHistorySources, home: Path, since: datetime) -> None:
    stamp = since.strftime("%Y-%m-%dT%H:%M:%SZ")
    for marker in sorted(home.glob(hs.git_clone_glob)):
        repo = marker.parent
        name = repo.name
        head = _git(
            ["-C", str(repo), "symbolic-ref", "-q", "refs/remotes/origin/HEAD"],
            hs.git_timeout_seconds,
        )
        if not head or not head.strip().startswith("refs/remotes/"):
            book.notes.append(
                f"{name}: no origin/HEAD in the canonical clone: its merges are unread"
            )
            continue
        ref = head.strip().removeprefix("refs/remotes/")
        out = _git(
            [
                "-C",
                str(repo),
                "log",
                "--first-parent",
                f"--since={stamp}",
                "--format=%ct",
                ref,
            ],
            hs.git_timeout_seconds,
        )
        if out is None:
            book.notes.append(f"{name}: git log of {ref} failed: its merges are unread")
            continue
        book.declare(SRC_GIT, "git_merges_total")
        book.declare(SRC_GIT, f"git_merges:{name}")
        for text in out.split():
            if text.isdigit():
                book.add_event(SRC_GIT, f"git_merges:{name}", float(text))
                book.add_event(SRC_GIT, "git_merges_total", float(text))


def load_book(
    hs: ModelHistorySources,
    env: Mapping[str, str],
    home: Path,
    since: datetime,
    loaders: tuple[str, ...] | None = None,
) -> Book:
    """Read every source from ``since`` on; one that fails leaves a note and its keys out."""
    book = Book()
    steps: dict[str, Callable[[], None]] = {
        SRC_LEDGER: lambda: load_ledger(book, hs, env, since),
        SRC_TICK_LOG: lambda: load_throughput(book, hs, home, since),
        SRC_FLOOR_ALARM: lambda: load_floor_alarm(book, hs, home, since),
        SRC_LANDING: lambda: load_landing(book, hs, env, since),
        SRC_LAB_ALARM: lambda: load_lab_alarms(book, hs, home, since),
        SRC_GIT: lambda: load_git(book, hs, home, since),
    }
    for name, step in steps.items():
        if loaders is None or name in loaders:
            step()
    book.finish()
    return book


def counts_at(
    book: Book, hs: ModelHistorySources, at: datetime, source: str
) -> dict[str, float]:
    """The counted keys of ``source`` over the window that ends at ``at``; empty when the source did not record in it."""
    end = at.timestamp()
    start = end - hs.window_hours * 3600
    if source in book.alive:
        times = book.alive[source]
        floor = hs.min_alive.get(source, 1)
        if (
            not times
            or _count(times, start, end) < floor
            or times[0] > start + _COVER_SLACK_S
        ):
            return {}
    elif source != SRC_GIT:
        return {}
    return {
        key: float(_count(book.events.get(key, []), start, end))
        for key in book.keys_of.get(source, [])
    }


def tick_mean_at(book: Book, hs: ModelHistorySources, at: datetime) -> dict[str, float]:
    """Lanes the lab fill sent per tick, over the ticks the log holds in the window (enough ticks, else unread)."""
    end = at.timestamp()
    start = end - hs.window_hours * 3600
    window = [sent for stamp, sent in book.tick_sent if start < stamp <= end]
    if len(window) < hs.min_tick_samples:
        return {}
    return {"lab_fill_sent_per_tick": sum(window) / len(window)}


@dataclass(frozen=True)
class FrictionRules:
    rules: tuple[core.ClassRule, ...]
    owners: dict[str, str]


def load_friction_rules(hs: ModelHistorySources) -> FrictionRules:
    path = Path(core.__file__).resolve().parents[1] / hs.friction_rules_file
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    rules = core.parse_class_rules(
        (item["name"], item["owner"], item["pattern"])
        for item in document["class_rules"]
    )
    owners = core.parse_class_owners(
        (item["class_name"], item["owner"])
        for item in (document.get("class_owners") or [])
    )
    return FrictionRules(rules=rules, owners=owners)


def class_title(name: str) -> str:
    return name.replace("-", " ").replace(":", ": ")


def _as_float(value: object) -> float:
    return float(value) if isinstance(value, (int, float, str)) else 0.0


def friction_values_from_classes(
    classes: list[Mapping[str, object]], hs: ModelHistorySources
) -> tuple[dict[str, float], dict[str, str]]:
    """The stoppage keys of one rollup window: occurrences and lane-minutes of every class, and the totals."""
    values: dict[str, float] = {}
    labels: dict[str, str] = {}
    rows = minutes = refusals = 0.0
    for item in classes:
        name = str(item["class"])
        occurrences, cost = (
            _as_float(item["occurrences"]),
            _as_float(item["cost_minutes"]),
        )
        cid = f"stoppage:{name}"
        values[f"cause:{cid}"], values[f"stoppage_cost:{cid}"] = occurrences, cost
        labels[f"cause:{cid}"] = labels[f"stoppage_cost:{cid}"] = class_title(name)
        rows += _as_float(item.get("rows", occurrences))
        minutes += cost
        if name.startswith(hs.refusal_class_prefix):
            refusals += occurrences
    values["friction_rows"], values["friction_lane_minutes"], values["refusals"] = (
        rows,
        minutes,
        refusals,
    )
    return values, labels


def friction_at(
    book: Book, hs: ModelHistorySources, at: datetime, rules: FrictionRules
) -> tuple[dict[str, float], dict[str, str]]:
    """The rollup's 24 hour window ending at ``at``, built by the rollup node's own report from the ledger's rows."""
    end = at.timestamp()
    start = end - hs.window_hours * 3600
    alive = book.alive.get(SRC_LEDGER, [])
    if (
        not alive
        or _count(alive, start, end) < hs.min_alive.get(SRC_LEDGER, 1)
        or alive[0] > start + _COVER_SLACK_S
    ):
        return {}, {}
    since, until = at - timedelta(hours=hs.window_hours), at
    selected = [row for row in book.friction_rows if since <= row.ts < until]
    report = core.build_report(
        selected,
        [],
        since=since,
        until=until,
        generated_at=until,
        bucket_hours=2,
        umbrella_tickets=frozenset({hs.friction_umbrella_ticket}),
        aliases={},
        duplicates_dropped=0,
        jsonl_provided=False,
        jsonl_malformed_lines=0,
        class_rules=rules.rules,
        class_owners=rules.owners,
    )
    classes = report["classes"]
    assert isinstance(classes, list)
    return friction_values_from_classes(classes, hs)


def grid(end: datetime, days: int, every_hours: int, minute: int = 0) -> list[datetime]:
    """Times ``every_hours`` apart at ``minute`` past the hour, newest at or before ``end``, back ``days`` days."""
    top = end.astimezone(UTC).replace(minute=minute, second=0, microsecond=0)
    if top > end:
        top -= timedelta(hours=1)
    top -= timedelta(hours=top.hour % every_hours)
    return [
        top - timedelta(hours=every_hours * step)
        for step in range(days * 24 // every_hours + 1)
    ]


def thin(
    samples: list[tuple[float, dict[str, float]]], gap_seconds: float
) -> list[tuple[float, dict[str, float]]]:
    out: list[tuple[float, dict[str, float]]] = []
    for stamp, values in samples:
        if not out or stamp - out[-1][0] >= gap_seconds:
            out.append((stamp, values))
    return out


def backfill_snapshots(
    book: Book,
    hs: ModelHistorySources,
    end: datetime,
    days: int,
    sample_gap_seconds: float,
    rules: FrictionRules,
) -> list[ModelSnapshot]:
    """Every dated point the sources can give for the ``days`` days before ``end``, each naming its source."""
    points: list[ModelSnapshot] = []
    for at in sorted(grid(end, days, hs.grid_hours)):
        for source in (SRC_LEDGER, SRC_GIT, SRC_LANDING, SRC_LAB_ALARM):
            values = counts_at(book, hs, at, source)
            if values:
                points.append(
                    ModelSnapshot(
                        at=at, source=f"{BACKFILL_PREFIX}{source}", values=values
                    )
                )
        mean = tick_mean_at(book, hs, at)
        if mean:
            points.append(
                ModelSnapshot(
                    at=at, source=f"{BACKFILL_PREFIX}{SRC_TICK_LOG}", values=mean
                )
            )
    low = (end - timedelta(days=days)).timestamp()
    for source in (SRC_TICK_LOG, SRC_FLOOR_ALARM):
        for stamp, values in thin(
            [s for s in book.samples.get(source, []) if s[0] >= low], sample_gap_seconds
        ):
            points.append(
                ModelSnapshot(
                    at=datetime.fromtimestamp(stamp, UTC),
                    source=f"{BACKFILL_PREFIX}{source}",
                    values=values,
                )
            )
    for at in sorted(grid(end, days, hs.friction_grid_hours, hs.friction_grid_minute)):
        values, labels = friction_at(book, hs, at, rules)
        if values:
            points.append(
                ModelSnapshot(
                    at=at,
                    source=f"{BACKFILL_PREFIX}{SRC_FRICTION}",
                    values=values,
                    labels=labels,
                    complete=FRICTION_PREFIXES,
                )
            )
    return points


def live_extension(
    hs: ModelHistorySources,
    env: Mapping[str, str],
    home: Path,
    now: datetime,
    latest_friction: Mapping[str, object] | None,
) -> tuple[dict[str, float], dict[str, str], tuple[str, ...], list[str]]:
    """Today's values of the same keys: the counted ones over the 24 hours that end now, the latest logged levels, and every
    stoppage class of the friction rollup's latest 24 hour window. Also the notes of what could not be read."""
    since = now - timedelta(hours=hs.window_hours + 1)
    book = load_book(hs, env, home, since)
    values: dict[str, float] = {}
    labels: dict[str, str] = {}
    for source in (SRC_LEDGER, SRC_GIT, SRC_LANDING, SRC_LAB_ALARM):
        values.update(counts_at(book, hs, now, source))
    values.update(tick_mean_at(book, hs, now))
    fresh = now.timestamp() - hs.level_max_age_minutes * 60
    for source in (SRC_TICK_LOG, SRC_FLOOR_ALARM):
        samples = book.samples.get(source, [])
        if samples and samples[-1][0] >= fresh:
            for key, number in samples[-1][1].items():
                if key not in (
                    "merges",
                    "open_prs",
                ):  # today's in-scope pair is the published floor's own
                    values[key] = number
    complete: tuple[str, ...] = ()
    notes = list(book.notes)
    window = (latest_friction or {}).get("windows")
    classes = window.get("24h", {}).get("classes") if isinstance(window, dict) else None
    if isinstance(classes, list):
        friction, titles = friction_values_from_classes(classes, hs)
        values.update(friction)
        labels.update(titles)
        complete = FRICTION_PREFIXES
    else:
        notes.append(
            "the friction rollup's latest 24 hour window is not readable: the stoppage classes are the floor's own"
        )
    return values, labels, complete, notes
