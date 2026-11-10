# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The factory floor's unresolved step (OMN-19998): golden chain, error chain, the parallel reads, the overlay and the seams.

Seams this step shares with other pieces, each pinned here so a change on either side fails a test:
  S1 step -> floor: the record the step writes is the file the floor's loader reads (path, model, margin).
  S2 step -> publisher: the publisher's step declares this module, this record path as its freshness file, and a freshness
     shorter than the floor's bound (tests/nodes/node_factory_floor_publish_effect/test_unresolved_step_seam.py).
  S3 overlay -> floor: the overlay's margin is not shorter than the floor's unresolved_max_age_hours.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.nodes.node_factory_floor_effect.models.model_unresolved_rows_record import (
    ModelUnresolvedRowsRecord,
)
from omnimarket.nodes.node_factory_floor_effect.source_loader import (
    OWN_CONTRACT_PATH,
    SourceRefusedError,
    load_contract,
    load_unresolved_rows,
)
from omnimarket.nodes.node_factory_floor_unresolved_effect import (
    runtime_factory_floor_unresolved as runtime,
)
from omnimarket.nodes.node_factory_floor_unresolved_effect.models.model_factory_floor_unresolved import (
    ModelFactoryFloorUnresolvedOverlay,
)

NOW = datetime(2026, 11, 10, 16, 0, tzinfo=UTC)
NODE_DIR = Path(runtime.__file__).parent
FLOOR = load_contract(OWN_CONTRACT_PATH).runtime
EXAMPLE = yaml.safe_load((NODE_DIR / "overlay.example.yaml").read_text())
OVERLAY = ModelFactoryFloorUnresolvedOverlay.model_validate(EXAMPLE)


def _ts(delta: timedelta) -> str:
    return (NOW - delta).strftime("%Y-%m-%dT%H:%M:%SZ")


WINDOW = timedelta(days=FLOOR.ledger_window_days)
MARGIN = timedelta(hours=OVERLAY.overlap_hours)
ROWS = [
    # Far older than the window: kept.
    f"L1 {_ts(WINDOW + timedelta(days=30))} | TERMINAL | lane=old-blocked | ticket=OMN-5 | outcome=blocked | blocker=waiting",
    f"L2 {_ts(WINDOW + timedelta(days=29))} | STATUS | lane=old-hand | ticket=OMN-6 | pr=kb#9 | handed-off=kb#9 | handed",
    # Inside the margin past the window's edge: kept (the record stays whole for the margin after it was read).
    f"L3 {_ts(WINDOW - timedelta(hours=1))} | TERMINAL | lane=edge-partial | ticket=OMN-7 | outcome=partial | partial",
    # Younger than the margin: the window read brings it, so the record does not.
    f"L4 {_ts(WINDOW - MARGIN - timedelta(hours=1))} | TERMINAL | lane=in-window | ticket=OMN-8 | outcome=blocked | blocked",
    # Not one of the four reads.
    f"L5 {_ts(WINDOW + timedelta(days=28))} | TERMINAL | lane=old-done | ticket=OMN-9 | outcome=done | done",
]


def _rows_file(tmp_path: Path) -> Path:
    path = tmp_path / "rows.txt"
    path.write_text("\n".join(ROWS) + "\n", encoding="utf-8")
    return path


def _home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


def _record_path(home: Path) -> Path:
    return home / FLOOR.sources.unresolved_rows_relpath


def test_golden_chain_writes_the_rows_older_than_the_margin_edge(
    tmp_path: Path,
) -> None:
    home, sink = _home(tmp_path), InMemoryEmissionSink()
    runtime.run(
        home,
        OVERLAY,
        sink,
        plugin_root=None,
        stand_in_file=str(_rows_file(tmp_path)),
        clock=lambda: NOW,
    )
    record = ModelUnresolvedRowsRecord.model_validate_json(
        _record_path(home).read_text()
    )
    assert {row.lane for row in record.rows} == {
        "old-blocked",
        "old-hand",
        "edge-partial",
    }
    assert record.covers_before == NOW - WINDOW + MARGIN
    assert record.valid_hours == OVERLAY.overlap_hours
    assert [e.phase for e in sink.events] == ["start", "success"]
    assert len({e.correlation_id for e in sink.events}) == 1
    assert sink.events[-1].counters["rows"] == 3


def test_error_chain_keeps_the_previous_record_when_a_read_fails(
    tmp_path: Path,
) -> None:
    home, sink = _home(tmp_path), InMemoryEmissionSink()
    previous = _record_path(home)
    previous.parent.mkdir(parents=True)
    previous.write_text("previous record", encoding="utf-8")
    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    # Break: the reader refuses every archive read. Without the guard the step would write an empty record.
    (plugin / "scripts/onex_ledger.py").write_text(
        "import sys\nsys.exit(3)\n", encoding="utf-8"
    )
    with pytest.raises(SourceRefusedError):
        runtime.run(home, OVERLAY, sink, plugin_root=str(plugin), clock=lambda: NOW)
    assert previous.read_text() == "previous record"
    assert [e.phase for e in sink.events] == ["start", "failure"]
    assert sink.events[-1].failure_code == "unresolved_read_failed"


def _reader_log_script(log: Path) -> str:
    return (
        "import sys, time\n"
        "if '--kind' in sys.argv:\n"
        f"    f = open({str(log)!r}, 'a')\n"
        "    f.write(f'start {time.monotonic()}\\n'); f.flush()\n"
        "    time.sleep(0.4)\n"
        "    f.write(f'end {time.monotonic()}\\n'); f.flush()\n"
    )


def _max_concurrent(log: Path) -> int:
    events = sorted(
        (float(line.split()[1]), 1 if line.startswith("start") else -1)
        for line in log.read_text().splitlines()
    )
    running = peak = 0
    for _, step in events:
        running += step
        peak = max(peak, running)
    return peak


@pytest.mark.parametrize(("workers", "expected_peak"), [(4, 4), (1, 1)])
def test_the_reads_run_together_as_many_as_the_overlay_allows(
    tmp_path: Path, workers: int, expected_peak: int
) -> None:
    """Break: a serial loop (the old floor) peaks at 1 whatever the overlay says; the workers=1 case is the failing control."""
    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    log = tmp_path / "reads.log"
    (plugin / "scripts/onex_ledger.py").write_text(
        _reader_log_script(log), encoding="utf-8"
    )
    overlay = OVERLAY.model_copy(update={"read_workers": workers})
    runtime.run(
        _home(tmp_path),
        overlay,
        InMemoryEmissionSink(),
        plugin_root=str(plugin),
        clock=lambda: NOW,
    )
    assert _max_concurrent(log) == expected_peak
    assert len(log.read_text().splitlines()) == 2 * len(OVERLAY.reads)


def test_the_overlay_example_names_the_four_reads_and_every_budget() -> None:
    assert [(r.kind, r.grep) for r in OVERLAY.reads] == [
        ("TERMINAL", "outcome=blocked"),
        ("TERMINAL", "outcome=partial"),
        ("TERMINAL", "outcome=handed-off"),
        ("STATUS", "handed-off="),
    ]
    assert OVERLAY.read_workers == len(OVERLAY.reads)


def test_main_exits_65_when_no_overlay_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("ONEX_FACTORY_FLOOR_UNRESOLVED_OVERLAY", raising=False)
    monkeypatch.setenv("ONEX_SKILL_OVERLAY_ROOTS", str(tmp_path / "no-overlays"))
    assert runtime.main(["--home", str(_home(tmp_path))]) == runtime.EXIT_UNREADABLE
    assert "overlay" in capsys.readouterr().err


def test_main_writes_the_record_and_exits_0(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    plugin = tmp_path / "plugin"
    (plugin / "scripts").mkdir(parents=True)
    rows = _rows_file(tmp_path)
    (plugin / "scripts/onex_ledger.py").write_text(
        f"import sys\nprint(open({str(rows)!r}).read())\n", encoding="utf-8"
    )
    overlay = tmp_path / "overlay.yaml"
    overlay.write_text(yaml.safe_dump(EXAMPLE), encoding="utf-8")
    monkeypatch.setenv("ONEX_FACTORY_FLOOR_UNRESOLVED_OVERLAY", str(overlay))
    monkeypatch.setenv(FLOOR.plugin_root_env, str(plugin))
    home = _home(tmp_path)
    assert runtime.main(["--home", str(home)]) == 0
    assert json.loads(capsys.readouterr().out)["rows"] >= 1
    assert _record_path(home).is_file()


# S1: the floor's loader reads what the step wrote, and refuses what the step's margin no longer covers.
def test_seam_the_floors_loader_reads_the_steps_record(tmp_path: Path) -> None:
    home = _home(tmp_path)
    runtime.run(
        home,
        OVERLAY,
        InMemoryEmissionSink(),
        plugin_root=None,
        stand_in_file=str(_rows_file(tmp_path)),
        clock=lambda: NOW,
    )
    later = NOW + timedelta(hours=2)
    rows = load_unresolved_rows(
        _record_path(home),
        now=later,
        window_days=FLOOR.ledger_window_days,
        max_age_hours=FLOOR.unresolved_max_age_hours,
    )
    # Two hours later the row that was an hour inside the window's edge has crossed it: it is the record's to bring now, and a
    # row still inside the window (L4) is not in the record at all (the window read brings it).
    assert {row.lane for row in rows} == {"old-blocked", "old-hand", "edge-partial"}
    with pytest.raises(SourceRefusedError, match="rerun the unresolved step"):
        load_unresolved_rows(
            _record_path(home),
            now=NOW + MARGIN + timedelta(minutes=1),
            window_days=FLOOR.ledger_window_days,
            max_age_hours=FLOOR.unresolved_max_age_hours,
        )


def test_seam_a_missing_record_is_refused_never_empty(tmp_path: Path) -> None:
    with pytest.raises(SourceRefusedError):
        load_unresolved_rows(
            tmp_path / "absent.json",
            now=NOW,
            window_days=FLOOR.ledger_window_days,
            max_age_hours=FLOOR.unresolved_max_age_hours,
        )


# S3: the floor's bound on the record's age is inside the margin the step reads.
def test_seam_the_floors_age_bound_is_inside_the_steps_margin() -> None:
    assert FLOOR.unresolved_max_age_hours <= OVERLAY.overlap_hours


def test_the_node_module_runs_as_python_dash_m_with_the_name_the_publisher_declares() -> (
    None
):
    assert sys.modules[runtime.__name__].NODE == "node_factory_floor_unresolved_effect"
