# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The running comparison of the floor's numbers against 1, 7 and 30 days ago (OMN-23018, AC2).

``build_comparison`` is pure: snapshots in, better and worse lines in plain words out. ``HandlerFloorCompare.run`` is the
cycle: read the published floor, keep its numbers in the history (backfilling dated copies once), build and write the
comparison, and emit a start and then a success or a typed failure under the floor's emission contract.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from omnimarket.handlers.cycle_interrupt import CycleInterrupted, interruptible
from omnimarket.handlers.emission_sink import (
    ChainEmitter,
    JsonlEmissionSink,
    ProtocolEmissionSink,
)
from omnimarket.handlers.node_overlay_reader import (
    NodeOverlayError,
    load_structured_overlay,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_floor_categories import (
    category_of,
    detail_rows,
    rollup,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_floor_history import (
    append_snapshot,
    prune,
    read_history,
    reading_at,
    rewrite_history,
    value_in,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_floor_metrics import (
    measures_from_factory_record,
    snapshot_from_factory_record,
    snapshot_from_floor_simple,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_floor_sources import (
    backfill_snapshots,
    live_extension,
    load_book,
    load_friction_rules,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_piece_measure import (
    append_verdicts,
    load_overlay,
    measure_pieces,
    read_verdicts,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_repository_names import (
    canonical_snapshot,
    canonical_snapshots,
    load_repository_names,
    unmapped_spellings,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelCompareLine,
    ModelFloorCompareOverlay,
    ModelFloorCompareRequest,
    ModelFloorCompareResult,
    ModelFloorCompareRuntime,
    ModelFloorComparison,
    ModelMetricFamily,
    ModelSnapshot,
    ModelWindowComparison,
)

NODE_DIR = Path(__file__).resolve().parents[1]
OWN_CONTRACT = NODE_DIR / "contract.yaml"
BACKFILL_MARKER = "backfill-done.json"
DRILL_PAYLOAD = b"{ not json"


def load_runtime(contract_path: Path = OWN_CONTRACT) -> ModelFloorCompareRuntime:
    raw = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
    return ModelFloorCompareRuntime.model_validate(raw["runtime"])


def family_of(key: str, rt: ModelFloorCompareRuntime) -> ModelMetricFamily | None:
    """The family whose prefix is the longest one ``key`` starts with."""
    hits = [f for f in rt.families if key == f.prefix or key.startswith(f.prefix)]
    return max(hits, key=lambda f: len(f.prefix), default=None)


def _text(
    value: float | None, family: ModelMetricFamily, rt: ModelFloorCompareRuntime
) -> str:
    if value is None:
        return "unread"
    if family.unit == "colour":
        names = {rank: name for name, rank in reversed(list(rt.piece_rank.items()))}
        return names.get(int(value), f"level {int(value)}")
    shown = f"{value:g}"
    return f"{shown} {family.unit}".rstrip()


def _polarity_text(family: ModelMetricFamily) -> str:
    return (
        "Higher is better"
        if family.polarity == "higher_is_better"
        else "Lower is better"
    )


def _line(
    key: str,
    old: float | None,
    new: float | None,
    old_at: datetime | None,
    window_label: str,
    family: ModelMetricFamily,
    noun: str,
    rt: ModelFloorCompareRuntime,
) -> ModelCompareLine:
    old_text, new_text = _text(old, family, rt), _text(new, family, rt)
    if old is None or new is None:
        verdict = "unread"
        which = f"{window_label} ago" if old is None else "now"
        words = f"{noun}: no reading {which}; {new_text} now, {old_text} {window_label} ago. Not counted as better or worse."
    elif old == new:
        verdict = "same"
        words = f"{noun}: {new_text} now, the same as {window_label} ago."
    else:
        improved = (new > old) == (family.polarity == "higher_is_better")
        verdict = "better" if improved else "worse"
        words = (
            f"{noun}: {new_text} now, was {old_text} {window_label} ago. "
            f"{_polarity_text(family)}, so this is {verdict}."
        )
    return ModelCompareLine(
        key=key,
        verdict=verdict,
        old=old,
        new=new,
        old_text=old_text,
        new_text=new_text,
        better_is=family.polarity,
        direction_text=_polarity_text(family),
        words=words,
        old_read_at=old_at,
        name=noun,
        unit=family.unit,
    )


def _size(line: ModelCompareLine) -> tuple[float, str]:
    if line.old is None or line.new is None:
        return (0.0, line.key)
    base = max(abs(line.old), 1.0)
    return (-abs(line.new - line.old) / base, line.key)


def build_comparison(
    snapshots: tuple[ModelSnapshot, ...],
    now: ModelSnapshot,
    rt: ModelFloorCompareRuntime,
    overlay: ModelFloorCompareOverlay | None = None,
    categories_error: str = "",
) -> ModelFloorComparison:
    """Every number now against the same number at now minus 1, 7 and 30 days; a missing point is unread, never zero.

    With the grouping ``overlay``, every line gets its category and each window carries one roll-up card per category and
    the whole window as details rows; without it the cards are absent and ``categories_error`` says why.
    """
    windows: list[ModelWindowComparison] = []
    for window in rt.windows:
        target = now.at - timedelta(days=window.days)
        tolerance = timedelta(hours=window.tolerance_hours)
        near = [s for s in snapshots if abs(s.at - target) <= tolerance]
        keys = sorted(set(now.values) | {k for s in near for k in s.values})
        lines: list[ModelCompareLine] = []
        for key in keys:
            family = family_of(key, rt)
            if family is None:
                continue
            reading = reading_at(snapshots, key, target, window.tolerance_hours)
            if family.prefix == key:
                noun = family.noun
            else:
                label = now.labels.get(key) or next(
                    (s.labels[key] for s in reversed(near) if key in s.labels),
                    key[len(family.prefix) :],
                )
                noun = f"{family.noun} {label}"
            line = _line(
                key,
                reading.value if reading.read else None,
                value_in(now, key),
                reading.at,
                window.label,
                family,
                noun,
                rt,
            )
            if overlay is not None:
                line = line.model_copy(
                    update={"category": category_of(key, overlay).id}
                )
            lines.append(line)
        by = {
            v: sorted([ln for ln in lines if ln.verdict == v], key=_size)
            for v in ("better", "worse", "same", "unread")
        }
        cap = rt.max_lines_per_window
        windows.append(
            ModelWindowComparison(
                days=window.days,
                label=window.label,
                target=target,
                better=tuple(by["better"][:cap]),
                worse=tuple(by["worse"][:cap]),
                same=len(by["same"]),
                unread=tuple(by["unread"][:cap]),
                counts={v: len(by[v]) for v in by},
                categories=rollup(lines, overlay, window.label) if overlay else (),
                details=detail_rows(lines),
            )
        )
    return ModelFloorComparison(
        generated_at=now.at,
        history_points=len(snapshots),
        oldest_point=snapshots[0].at if snapshots else None,
        windows=tuple(windows),
        details_page_size=rt.details_page_size,
        categories_error=categories_error,
    )


def _write_atomic(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    tmp.replace(path)


class HandlerFloorCompare:
    """Boundary of the cycle: the reads, the history file and the written comparison."""

    def __init__(
        self,
        sink: ProtocolEmissionSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._sink = sink
        self._clock = clock or (lambda: datetime.now(UTC))

    def handle(self, request: ModelFloorCompareRequest) -> ModelFloorCompareResult:
        return self.run(request)

    def run(
        self,
        request: ModelFloorCompareRequest,
        *,
        environ: Mapping[str, str] | None = None,
    ) -> ModelFloorCompareResult:
        rt = load_runtime()
        env = os.environ if environ is None else environ
        home_value = (request.home or env.get(rt.home_env, "")).strip()
        if not home_value:
            return ModelFloorCompareResult(
                ok=False,
                failure_code="home_not_set",
                failure_detail=f"{rt.home_env} is not set (fail fast, no default)",
            )
        home = Path(home_value)
        now = request.now or self._clock()
        sink = self._sink or JsonlEmissionSink(home / rt.emission_journal_relpath)
        chain = ChainEmitter(
            sink,
            node=rt.emission_node,
            piece=rt.emission_piece,
            clock=self._clock,
            drill=request.drill_error,
        )
        # From the start, every way out ends in a success or a typed failure (OMN-22864): an error, a termination signal and
        # the cycle's own deadline each close the start. The close is outside the signal span so a late signal cannot cut it.
        try:
            with interruptible(rt.cycle_deadline_seconds):
                chain.start()
                return self._cycle(request, rt, home, now, chain, env)
        except CycleInterrupted as exc:
            if chain.open:
                chain.failure(exc.code, exc.detail)
            raise
        except BaseException as exc:
            if chain.open:
                chain.failure("cycle_interrupted", f"{type(exc).__name__}: {exc}")
            raise

    def _cycle(
        self,
        request: ModelFloorCompareRequest,
        rt: ModelFloorCompareRuntime,
        home: Path,
        now: datetime,
        chain: ChainEmitter,
        env: Mapping[str, str],
    ) -> ModelFloorCompareResult:
        history_path = home / rt.history_relpath
        raw = DRILL_PAYLOAD if request.drill_error else None
        doc = (
            self._read_json(home / rt.simple_relpath)
            if raw is None
            else self._parse(raw)
        )
        snapshot = (
            snapshot_from_floor_simple(doc, rt, source="tick")
            if doc is not None
            else None
        )
        if snapshot is None and request.backfill_only and raw is None:
            # Archive-only operation reads the dated copies into the history and records no tick: it needs no live floor.
            archived, bad_lines = read_history(history_path)
            imported = self._backfill(rt, home, history_path, archived)
            wrote_by_source: dict[str, int] = {}
            source_notes: tuple[str, ...] = ()
            if request.backfill_history:
                wrote_by_source, source_notes = self._backfill_history(
                    request, rt, home, now, env, history_path
                )
            points = len(read_history(history_path)[0])
            chain.success(
                {
                    "history_points": points,
                    "backfilled": imported,
                    "recorded": 0,
                    "skipped_lines": bad_lines,
                    "history_written": sum(wrote_by_source.values()),
                    "source_notes": len(source_notes),
                }
            )
            return ModelFloorCompareResult(
                ok=True,
                backfilled=imported,
                history_points=points,
                history_written=wrote_by_source,
                source_notes=source_notes,
            )
        if snapshot is None:
            chain.failure(
                "simple_payload_invalid",
                f"{rt.simple_relpath} is missing, not JSON or has no generated_at; nothing recorded and no comparison written",
            )
            return ModelFloorCompareResult(
                ok=False,
                failure_code="simple_payload_invalid",
                failure_detail=f"{rt.simple_relpath} cannot be read",
            )
        snapshot, notes = self._with_sources(snapshot, rt, home, env)
        record = self._read_json(home / rt.record_relpath) if raw is None else None
        measured = (
            measures_from_factory_record(record, rt, source="tick")
            if record is not None
            else None
        )
        if measured is not None and measured.at.date() == snapshot.at.date():
            snapshot = snapshot.model_copy(
                update={"values": {**snapshot.values, **measured.values}}
            )
        snapshots, skipped = read_history(history_path)
        added = self._backfill(rt, home, history_path, snapshots)
        written: dict[str, int] = {}
        if request.backfill_history:
            written, more = self._backfill_history(
                request, rt, home, now, env, history_path
            )
            notes = (*notes, *more)
        if added or written:
            snapshots, skipped = read_history(history_path)
        recorded = False
        same_source = [s for s in snapshots if s.source == "tick"]
        if not request.backfill_only and (
            not same_source
            or (snapshot.at - same_source[-1].at).total_seconds() >= rt.min_gap_seconds
        ):
            append_snapshot(history_path, snapshot)
            snapshots = (*snapshots, snapshot)
            recorded = True
        kept = prune(snapshots, now, rt.retention_days)
        if len(kept) != len(snapshots):
            rewrite_history(history_path, kept)
            snapshots = kept
        snapshots = tuple(sorted(snapshots, key=lambda s: s.at))
        snapshots, snapshot, name_notes = self._canonical_names(
            snapshots, snapshot, rt, home, env
        )
        notes = (*notes, *name_notes)
        overlay, categories_error = self._grouping(request, rt, env)
        comparison = build_comparison(
            snapshots, snapshot, rt, overlay, categories_error
        )
        comparison, verdicts_written, more = self._pieces(
            comparison, snapshots, home, now, env
        )
        notes = (*notes, *more)
        out = home / rt.output_relpath
        _write_atomic(out, comparison.model_dump_json(indent=1).encode("utf-8"))
        chain.success(
            {
                "history_points": len(snapshots),
                "backfilled": added,
                "recorded": int(recorded),
                "skipped_lines": skipped,
                "unread_lines_1d": comparison.windows[0].counts["unread"],
                "categories_drawn": int(overlay is not None),
                "history_written": sum(written.values()),
                "source_notes": len(notes),
                "pieces_measured": len(comparison.pieces),
                "verdicts_recorded": verdicts_written,
            }
        )
        return ModelFloorCompareResult(
            ok=True,
            wrote=rt.output_relpath,
            recorded=recorded,
            backfilled=added,
            history_points=len(snapshots),
            history_written=written,
            source_notes=notes,
            categories_error=categories_error,
        )

    @staticmethod
    def _canonical_names(
        snapshots: tuple[ModelSnapshot, ...],
        snapshot: ModelSnapshot,
        rt: ModelFloorCompareRuntime,
        home: Path,
        env: Mapping[str, str],
    ) -> tuple[tuple[ModelSnapshot, ...], ModelSnapshot, tuple[str, ...]]:
        """Every per-repository key under the registry's canonical name (OMN-23134); the history file keeps what each source spelled."""
        try:
            names, notes = load_repository_names(
                env, home, rt.repository_names_overlay_relpath
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return (
                snapshots,
                snapshot,
                (
                    f"the repository names overlay cannot be read ({type(exc).__name__}: {exc}); repositories stay under each source's spelling",
                ),
            )
        today = canonical_snapshot(snapshot, names)
        return (
            canonical_snapshots(snapshots, names),
            today,
            (*notes, *unmapped_spellings(today, names)),
        )

    @staticmethod
    def _grouping(
        request: ModelFloorCompareRequest,
        rt: ModelFloorCompareRuntime,
        env: Mapping[str, str],
    ) -> tuple[ModelFloorCompareOverlay | None, str]:
        """The category grouping from the deployment overlay, or why there is none.

        A missing or invalid overlay never stops the history from being kept: the comparison is still written, without the
        cards, and says why, so the page can show the reason instead of a wrong grouping.
        """
        try:
            return (
                load_structured_overlay(
                    rt.overlay_id,
                    ModelFloorCompareOverlay,
                    env,
                    explicit=request.overlay,
                    file_env=rt.overlay_file_env,
                ),
                "",
            )
        except NodeOverlayError as exc:
            return None, str(exc)

    @staticmethod
    def _pieces(
        comparison: ModelFloorComparison,
        snapshots: tuple[ModelSnapshot, ...],
        home: Path,
        now: datetime,
        env: Mapping[str, str],
    ) -> tuple[ModelFloorComparison, int, tuple[str, ...]]:
        """Each live piece against its baseline (OMN-23081); a day-7 verdict is recorded once and read back unchanged."""
        try:
            overlay = load_overlay(env)
        except (OSError, ValueError, KeyError) as exc:
            return (
                comparison,
                0,
                (
                    f"the piece measures overlay cannot be read ({type(exc).__name__}: {exc}); no piece is measured",
                ),
            )
        path = home / overlay.verdicts_relpath
        results, fresh = measure_pieces(snapshots, overlay, now, read_verdicts(path))
        written = append_verdicts(path, fresh)
        return (
            comparison.model_copy(
                update={"pieces": tuple(r.model_dump(mode="json") for r in results)}
            ),
            written,
            (),
        )

    @staticmethod
    def _with_sources(
        snapshot: ModelSnapshot,
        rt: ModelFloorCompareRuntime,
        home: Path,
        env: Mapping[str, str],
    ) -> tuple[ModelSnapshot, tuple[str, ...]]:
        """Today's value of every dated-source key is added to the tick's snapshot; a source that cannot be read adds a note."""
        hs = rt.history
        latest: Mapping[str, object] | None = None
        try:
            doc = json.loads((home / hs.friction_latest_relpath).read_bytes())
            latest = doc if isinstance(doc, dict) else None
        except (OSError, ValueError):
            latest = None
        try:
            values, labels, complete, notes = live_extension(
                hs, env, home, snapshot.at, latest
            )
        except (OSError, ValueError, KeyError) as exc:
            return snapshot, (
                f"the dated sources could not be read for today ({type(exc).__name__}: {exc})",
            )
        return (
            snapshot.model_copy(
                update={
                    "values": {**snapshot.values, **values},
                    "labels": {**snapshot.labels, **labels},
                    "complete": tuple(dict.fromkeys((*snapshot.complete, *complete))),
                }
            ),
            tuple(notes),
        )

    @staticmethod
    def _backfill_history(
        request: ModelFloorCompareRequest,
        rt: ModelFloorCompareRuntime,
        home: Path,
        now: datetime,
        env: Mapping[str, str],
        history_path: Path,
    ) -> tuple[dict[str, int], tuple[str, ...]]:
        """Write the dated points the sources hold for the days before ``now``; a point already stored (same source and time) is kept."""
        hs = rt.history
        days = request.history_days or hs.backfill_days
        book = load_book(
            hs,
            env,
            home,
            now - timedelta(hours=days * 24 + hs.window_hours + 1),
            until=now,
        )
        points = backfill_snapshots(
            book, hs, now, days, rt.min_gap_seconds, load_friction_rules(hs)
        )
        have = {(s.source, s.at) for s in read_history(history_path)[0]}
        written: dict[str, int] = {}
        for point in sorted(points, key=lambda s: s.at):
            if (point.source, point.at) in have:
                continue
            have.add((point.source, point.at))
            append_snapshot(history_path, point)
            written[point.source] = written.get(point.source, 0) + 1
        return written, tuple(book.notes)

    @staticmethod
    def _parse(raw: bytes) -> dict[str, Any] | None:
        try:
            doc = json.loads(raw)
        except ValueError:
            return None
        return doc if isinstance(doc, dict) else None

    def _read_json(self, path: Path) -> dict[str, Any] | None:
        try:
            return self._parse(path.read_bytes())
        except OSError:
            return None

    def _backfill(
        self,
        rt: ModelFloorCompareRuntime,
        home: Path,
        history_path: Path,
        existing: tuple[ModelSnapshot, ...],
    ) -> int:
        """Read each dated copy the overlay-free contract lists once; a copy that does not parse is skipped, never counted."""
        marker_path = history_path.with_name(BACKFILL_MARKER)
        done: set[str] = set()
        try:
            done = set(json.loads(marker_path.read_text(encoding="utf-8")))
        except (OSError, ValueError):
            done = set()
        have = {(s.source, s.at) for s in existing}
        fresh: list[ModelSnapshot] = []
        parsers = {
            "floor_simple": snapshot_from_floor_simple,
            "factory_record": snapshot_from_factory_record,
            "factory_measures": measures_from_factory_record,
        }
        for source in rt.backfill:
            parse = parsers[source.kind]
            for path in sorted(home.glob(source.glob)):
                rel = path.relative_to(home).as_posix()
                # A kind added after the first backfill reads the same copies again under its own mark (OMN-23081).
                if source.kind == "factory_measures":
                    rel = f"{source.kind}:{rel}"
                if rel in done or not path.is_file():
                    continue
                doc = self._read_json(path)
                snap = parse(doc, rt, source=f"backfill:{source.kind}") if doc else None
                if snap is None:
                    # Unreadable or incomplete now: not checkpointed, so a copy that becomes valid is still imported.
                    continue
                done.add(rel)
                if (snap.source, snap.at) in have or not snap.values:
                    continue
                have.add((snap.source, snap.at))
                fresh.append(snap)
        for snap in sorted(fresh, key=lambda s: s.at):
            append_snapshot(history_path, snap)
        if done:
            _write_atomic(marker_path, json.dumps(sorted(done)).encode("utf-8"))
        return len(fresh)
