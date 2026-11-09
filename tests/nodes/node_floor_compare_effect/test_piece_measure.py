# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""Each live rebuild piece against the old way over its first 7 days (OMN-23081).

A planted better history and a planted worse one each give their verdict on day 7; a thin baseline reads unread, never a
guess; before day 7 there is no verdict; a recorded verdict is read back unchanged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_floor_metrics import (
    measures_from_factory_record,
    snapshot_from_floor_simple,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_floor_sources import (
    Book,
    counts_at,
    load_ledger,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.adapter_piece_measure import (
    OVERLAY_ENV,
    load_overlay,
    measure_pieces,
    read_verdicts,
)
from omnimarket.nodes.node_floor_compare_effect.handlers.handler_floor_compare import (
    HandlerFloorCompare,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_floor_compare import (
    ModelFloorCompareRequest,
    ModelFloorComparison,
    ModelSnapshot,
)
from omnimarket.nodes.node_floor_compare_effect.models.model_piece_measure import (
    ModelPieceMeasuresOverlay,
    ModelPieceVerdictRecord,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    InMemoryLedger,
)
from tests.nodes.node_floor_compare_effect.support import RT, build_home, served_floor

pytestmark = pytest.mark.unit

LIVE = datetime(2026, 11, 6, 15, 0, tzinfo=UTC)
DAY7 = LIVE + timedelta(days=7, hours=1)


def overlay(**kw: object) -> ModelPieceMeasuresOverlay:
    raw = {
        "baseline_days": 7,
        "trial_days": 7,
        "min_points": 3,
        "verdicts_relpath": ".onex_state/floor-compare/piece-verdicts.jsonl",
        "pieces": [
            {
                "piece": 2,
                "name": "Hosts and capacity",
                "call": "wire",
                "live_at": LIVE.isoformat(),
                "live_source": "planted",
                "measures": [
                    {
                        "key": "slots_free",
                        "label": "Lab slots available",
                        "unit": "slots free",
                        "polarity": "higher_is_better",
                        "source": "planted",
                    },
                    {
                        "key": "capacity_refusals",
                        "label": "Lanes refused for capacity",
                        "unit": "rows in 24 h",
                        "polarity": "lower_is_better",
                        "source": "planted",
                    },
                ],
            }
        ],
    }
    raw.update(kw)
    return ModelPieceMeasuresOverlay.model_validate(raw)


def history(
    before: dict[str, float], after: dict[str, float], every_h: int = 6
) -> tuple[ModelSnapshot, ...]:
    """A point every ``every_h`` hours for 7 days before the live date and 7 days after it."""
    points = []
    for h in range(-7 * 24, 7 * 24, every_h):
        at = LIVE + timedelta(hours=h)
        points.append(
            ModelSnapshot(at=at, source="tick", values=dict(before if h < 0 else after))
        )
    return tuple(points)


def test_planted_better_history_reads_better_on_day_7() -> None:
    snaps = history(
        {"slots_free": 10, "capacity_refusals": 9},
        {"slots_free": 20, "capacity_refusals": 2},
    )
    (res,), fresh = measure_pieces(snaps, overlay(), DAY7, {})
    assert res.verdict == "better"
    assert [m.verdict for m in res.measures] == ["better", "better"]
    assert res.measures[0].baseline == 10
    assert res.measures[0].trial == 20
    assert len(res.measures[0].days) == 7
    assert [r.verdict for r in fresh] == ["better"]


def test_planted_worse_history_reads_worse_on_day_7() -> None:
    snaps = history(
        {"slots_free": 20, "capacity_refusals": 2},
        {"slots_free": 12, "capacity_refusals": 7},
    )
    (res,), fresh = measure_pieces(snaps, overlay(), DAY7, {})
    assert res.verdict == "worse"
    assert [m.verdict for m in res.measures] == ["worse", "worse"]
    assert [r.verdict for r in fresh] == ["worse"]


def test_failing_control_one_worse_measure_makes_the_piece_worse() -> None:
    snaps = history(
        {"slots_free": 10, "capacity_refusals": 2},
        {"slots_free": 20, "capacity_refusals": 7},
    )
    (res,), _ = measure_pieces(snaps, overlay(), DAY7, {})
    assert [m.verdict for m in res.measures] == ["better", "worse"]
    assert res.verdict == "worse"


def test_a_baseline_the_history_does_not_hold_is_unread_never_inferred() -> None:
    snaps = tuple(
        s
        for s in history({"slots_free": 10}, {"slots_free": 20, "capacity_refusals": 1})
        if s.at >= LIVE
    )
    (res,), fresh = measure_pieces(snaps, overlay(), DAY7, {})
    assert res.verdict == "unread"
    assert all(m.verdict == "unread" for m in res.measures)
    assert res.measures[0].baseline is None
    assert res.measures[0].baseline_points == 0
    assert [r.verdict for r in fresh] == ["unread"]


def test_a_trial_with_too_few_points_is_unread() -> None:
    snaps = history(
        {"slots_free": 10, "capacity_refusals": 9},
        {"slots_free": 20, "capacity_refusals": 2},
        every_h=6,
    )
    thin = tuple(s for s in snaps if s.at < LIVE or s.at < LIVE + timedelta(hours=12))
    (res,), _ = measure_pieces(thin, overlay(), DAY7, {})
    assert res.verdict == "unread"


def test_before_day_7_there_is_no_verdict_only_the_days_so_far() -> None:
    snaps = history(
        {"slots_free": 10, "capacity_refusals": 9},
        {"slots_free": 20, "capacity_refusals": 2},
    )
    now = LIVE + timedelta(days=2, hours=3)
    (res,), fresh = measure_pieces(snaps, overlay(), now, {})
    assert res.verdict == "pending"
    assert res.verdict_at is None
    assert res.day == 3
    assert len(res.measures[0].days) == 3
    assert all(m.verdict is None for m in res.measures)
    assert fresh == ()


def test_a_recorded_verdict_is_kept_as_recorded(tmp_path: Path) -> None:
    rec = ModelPieceVerdictRecord(
        piece=2,
        live_at=LIVE,
        verdict="worse",
        verdict_at=DAY7,
        reason="recorded",
        measures={},
    )
    snaps = history(
        {"slots_free": 10, "capacity_refusals": 9},
        {"slots_free": 20, "capacity_refusals": 2},
    )
    (res,), fresh = measure_pieces(
        snaps, overlay(), DAY7 + timedelta(days=3), {(2, LIVE): rec}
    )
    assert res.verdict == "worse"
    assert res.recorded
    assert fresh == ()


def test_the_shipped_overlay_declares_pieces_1_2_3_5_and_6() -> None:
    ov = load_overlay({})
    assert [p.piece for p in ov.pieces] == [1, 2, 3, 5, 6]
    for p in ov.pieces:
        assert p.live_at.tzinfo is not None
        assert p.live_source
        assert p.measures
    keys = {m.key for p in ov.pieces for m in p.measures}
    assert {
        "slots_free",
        "capacity_refusals",
        "ledger_read_lag_s",
        "parity_failures",
    } <= keys
    # Every declared key belongs to a compare family, so the history can hold it.
    prefixes = [f.prefix for f in RT.families]
    assert all(any(k.startswith(px) for px in prefixes) for k in keys)


def test_factory_record_gives_slots_free_and_ledger_lag() -> None:
    doc = {
        "generated_at": "2026-11-08T05:07:56Z",
        "sources": {"ledger_newest_row": "2026-11-08T05:07:27Z"},
        "floor": {
            "bays": [
                {"host": "a", "free": 20, "stale": False},
                {"host": "b", "free": 3, "stale": False},
            ]
        },
    }
    snap = measures_from_factory_record(doc, RT, source="t")
    assert snap is not None
    assert snap.values == {"slots_free": 23.0, "ledger_read_lag_s": 29.0}
    doc["floor"]["bays"][1]["stale"] = True
    del doc["sources"]
    assert measures_from_factory_record(doc, RT, source="t").values == {
        "slots_free": 20.0
    }
    doc["floor"]["bays"][0]["stale"] = True
    assert measures_from_factory_record(doc, RT, source="t").values == {}


def test_floor_simple_gives_each_piece_golden_age() -> None:
    doc = served_floor()
    snap = snapshot_from_floor_simple(doc, RT, source="t")
    assert snap is not None
    ages = {p["piece"]: p.get("golden_age_s") for p in doc["pieces"]}
    for n, age in ages.items():
        key = f"piece_golden_age_h:{n}"
        assert (key in snap.values) == (age is not None)


def test_ledger_patterns_count_capacity_refusals_and_parity_failures(
    tmp_path: Path,
) -> None:
    rows = []
    base = datetime(2026, 11, 7, 0, 0, tzinfo=UTC)
    for i in range(120):
        at = (base + timedelta(minutes=10 * i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows.append(f"{at} | STATUS | lane=x{i} | ticket=OMN-1 | plain row")
    rows.append(
        "2026-11-07T12:00:00Z | FRICTION | lane=a | ticket=OMN-1 | cost=1h | the run exited 75: every host over the bar"
    )
    rows.append(
        "2026-11-07T12:05:00Z | STATUS | lane=b | ticket=OMN-1 | parity read FAIL on kind STATUS"
    )
    rows.append(
        "2026-11-07T12:06:00Z | CLAIM | lane=c | ticket=OMN-1 | parity failure noted in a claim is not counted"
    )
    book = Book()
    load_ledger(
        book,
        RT.history,
        {},
        base - timedelta(hours=1),
        base + timedelta(hours=24),
        InMemoryLedger(rows),
    )
    book.finish()
    got = counts_at(book, RT.history, base + timedelta(hours=23), "ledger")
    assert got["capacity_refusals"] == 1.0
    assert got["parity_failures"] == 1.0


def test_the_cycle_writes_each_piece_and_records_the_day_7_verdict_once(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path, served_floor(DAY7))
    hist = home / RT.history_relpath
    hist.parent.mkdir(parents=True, exist_ok=True)
    with hist.open("w", encoding="utf-8") as handle:
        for s in history(
            {"slots_free": 10, "capacity_refusals": 9},
            {"slots_free": 20, "capacity_refusals": 2},
        ):
            handle.write(s.model_dump_json() + "\n")
    ov_path = tmp_path / "overlay.yaml"
    ov_path.write_text(
        yaml.safe_dump({"piece_measures": json.loads(overlay().model_dump_json())}),
        encoding="utf-8",
    )
    env = {OVERLAY_ENV: str(ov_path)}
    sink = InMemoryEmissionSink()
    req = ModelFloorCompareRequest(home=str(home), now=DAY7)
    result = HandlerFloorCompare(sink=sink, clock=lambda: DAY7).run(req, environ=env)
    assert result.ok
    out = ModelFloorComparison.model_validate_json(
        (home / RT.output_relpath).read_text()
    )
    assert [(p["piece"], p["verdict"]) for p in out.pieces] == [(2, "better")]
    assert out.pieces[0]["live_at"].startswith("2026-11-06T15:00:00")
    assert out.pieces[0]["measures"][0]["baseline"] == 10
    verdicts = home / ".onex_state/floor-compare/piece-verdicts.jsonl"
    assert len(read_verdicts(verdicts)) == 1
    later = DAY7 + timedelta(hours=2)
    HandlerFloorCompare(sink=InMemoryEmissionSink(), clock=lambda: later).run(
        ModelFloorCompareRequest(home=str(home), now=later), environ=env
    )
    assert len(verdicts.read_text().splitlines()) == 1
    again = ModelFloorComparison.model_validate_json(
        (home / RT.output_relpath).read_text()
    )
    assert again.pieces[0]["recorded"] is True
    assert again.pieces[0]["verdict"] == "better"


def test_error_an_unreadable_overlay_measures_no_piece_and_says_so(
    tmp_path: Path,
) -> None:
    home = build_home(tmp_path)
    bad = tmp_path / "bad.yaml"
    bad.write_text("piece_measures: {pieces: [{piece: 99}]}\n", encoding="utf-8")
    result = HandlerFloorCompare(sink=InMemoryEmissionSink()).run(
        ModelFloorCompareRequest(home=str(home)), environ={OVERLAY_ENV: str(bad)}
    )
    assert result.ok
    assert any(
        "piece measures overlay cannot be read" in n for n in result.source_notes
    )
    out = ModelFloorComparison.model_validate_json(
        (home / RT.output_relpath).read_text()
    )
    assert out.pieces == ()
