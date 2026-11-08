# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The floor_simple publisher step stays well inside its 15 s limit (OMN-23081).

The publisher gives ``floor_simple`` 15 s. Measured on 2026-11-08 it took 14.1 to 14.6 s on the launching Mac, and the
Mac publisher refused many ticks on step timeouts, which left the served floor stale. Profiled on a lab host over the
recorded inputs, almost all of the time was the ledger read: ``FileLedgerRowSource.read`` parsed every line of the live
ledger and of each roll under the lookback (cells split, fields indexed) once per query, and the floor asks one query per
(row type, lane) pair of its overlay plus the go rulings, the readings and each shadow rule.

These tests hold the fix: the ledger is read once per cycle whatever the number of queries, rows of other types are never
parsed, and the cycle over a ledger the size of the recorded one finishes under a bound well below the step limit.
The ledger here is generated to the size of the one the publisher read (126,000 rows in the window); the rows the floor
counts are the recorded rows of the fixtures.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from omnimarket.handlers.emission_sink import InMemoryEmissionSink
from omnimarket.models.floor_17a import (
    ModelFloorSimple,
    ModelFloorSimpleOverlay,
    ModelFloorSimpleTickRequest,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.handler_floor_simple_tick import (
    HandlerFloorSimpleTick,
)
from omnimarket.nodes.node_floor_simple_effect.handlers.ledger_reads import (
    read_ledger_facts,
)
from omnimarket.nodes.node_reading_writer_effect.adapters.ledger_adapters import (
    FileLedgerRowSource,
    InMemoryLedger,
)
from omnimarket.nodes.node_reading_writer_effect.protocols import (
    ModelLedgerQuery,
    ModelLedgerRead,
)
from tests.nodes.node_floor_simple_effect.support import (
    build_home,
    environ,
    overlay_text,
)

pytestmark = pytest.mark.unit

FIXTURES = Path(__file__).parent / "fixtures"
AT = datetime(2026, 11, 6, 19, 0, tzinfo=UTC)

# The ledger the publisher read had 126,000 rows inside the window.
LEDGER_ROWS = 126_000
ROLLS = 6
# The step limit is 15 s. The bound is a third of it: on a lab host the fixed ledger read takes about a second, and the
# unfixed one took over a minute, so the bound sits between the two with room for a loaded host.
LEDGER_READ_BOUND_S = 5.0
CYCLE_BOUND_S = 5.0


def event_key(e: Any) -> tuple[Any, Any, Any]:
    return (e.emitted_at, e.correlation_id, e.phase)


FILLER_TYPES = ("STATUS", "CLAIM", "TERMINAL", "MSG", "FRICTION", "ACK")


def overlay() -> ModelFloorSimpleOverlay:
    return ModelFloorSimpleOverlay.model_validate(
        overlay_text("node_floor_simple_compute")
    )


def recorded_rows() -> list[str]:
    rows: list[str] = []
    for name in (
        "ledger_rows_go_and_code_graph.txt",
        "ledger_rows_piece_events.txt",
        "ledger_rows_reading_writer.txt",
    ):
        rows.extend(
            x
            for x in (FIXTURES / name).read_text(encoding="utf-8").splitlines()
            if x.strip()
        )
    return rows


def filler_row(index: int) -> str:
    """One row of the size and shape of a lane's STATUS or CLAIM row, spread over the 28 days before ``AT``."""
    stamp = (AT - timedelta(seconds=(index * 19) % (28 * 86400))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    kind = FILLER_TYPES[index % len(FILLER_TYPES)]
    return (
        f"{stamp} | {kind} | lane=filler-{index % 400} | ticket=OMN-{20000 + index % 900} | "
        f"actor=claude:sonnet:subagent | model=sonnet | band=B3 | repo=omnimarket | "
        f"worktree=omni_worktrees/OMN-{20000 + index % 900}/omnimarket | pr=omnimarket#{index % 700} | "
        f"scope=filler row {index} of the generated ledger, long enough to look like a lane row with its prose | "
        f"est ~1 lane-hours; displaces nothing; (OMN-{20000 + index % 900})"
    )


def write_ledger(root: Path, total: int = LEDGER_ROWS) -> Path:
    """A live ledger and ``ROLLS`` rolls beside it holding ``total`` rows, the recorded rows among them."""
    live = root / "ROLLING_WORK_LEDGER.md"
    archive = root / "archive"
    archive.mkdir(parents=True)
    recorded = recorded_rows()
    per_file = total // (ROLLS + 1)
    files = [
        live,
        *(archive / f"ROLLING_WORK_LEDGER_roll-{n}.md" for n in range(ROLLS)),
    ]
    for number, path in enumerate(files):
        body = [filler_row(number * per_file + i) for i in range(per_file)]
        if number == 0:
            body.extend(recorded)
        path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return live


def route_env(home: Path, live: Path) -> dict[str, str]:
    return {**environ(home), "ONEX_LEDGER_PATH": str(live)}


@pytest.fixture(scope="module")
def big_ledger(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return write_ledger(tmp_path_factory.mktemp("ledger"))


def count_opens(monkeypatch: pytest.MonkeyPatch, live: Path) -> dict[str, int]:
    """Count the opens of the ledger files by any reader, by file name."""
    opened: dict[str, int] = {}
    real_open = Path.open

    def counting(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name.startswith("ROLLING_WORK_LEDGER"):
            opened[self.name] = opened.get(self.name, 0) + 1
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting)
    return opened


def test_the_ledger_is_read_once_per_cycle_whatever_the_number_of_queries(
    big_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened = count_opens(monkeypatch, big_ledger)
    source = FileLedgerRowSource(big_ledger, big_ledger.parent / "archive")
    result = read_ledger_facts(source, overlay(), AT)
    assert result.error is None
    assert result.facts is not None
    # The live file and each roll are opened once, not once per query.
    assert len(opened) == ROLLS + 1
    assert set(opened.values()) == {1}, opened


def test_the_ledger_read_over_the_recorded_size_finishes_under_the_bound(
    big_ledger: Path,
) -> None:
    source = FileLedgerRowSource(big_ledger, big_ledger.parent / "archive")
    started = time.perf_counter()
    result = read_ledger_facts(source, overlay(), AT)
    elapsed = time.perf_counter() - started
    assert result.facts is not None
    assert elapsed < LEDGER_READ_BOUND_S, (
        f"the ledger read took {elapsed:.1f} s over {LEDGER_ROWS} rows; the bound is {LEDGER_READ_BOUND_S} s "
        "(the publisher's step limit is 15 s)"
    )


class PerQuerySource:
    """A source with no single-pass read: the floor asks it one query at a time, the way it did before OMN-23081."""

    def __init__(self, inner: FileLedgerRowSource) -> None:
        self._inner = inner

    def read(self, query: ModelLedgerQuery) -> ModelLedgerRead:
        return self._inner.read(query)


def test_the_single_pass_read_gives_the_same_facts_as_one_read_per_query(
    tmp_path: Path,
) -> None:
    """Parity with the old way: the same facts from the same rows, whether read once or query by query."""
    live = write_ledger(tmp_path / "small", total=7_000)
    archive = live.parent / "archive"
    once = read_ledger_facts(FileLedgerRowSource(live, archive), overlay(), AT)
    per_query = read_ledger_facts(
        PerQuerySource(FileLedgerRowSource(live, archive)), overlay(), AT
    )
    in_memory = read_ledger_facts(
        InMemoryLedger(
            [
                line
                for path in [live, *sorted(archive.iterdir())]
                for line in path.read_text(encoding="utf-8").splitlines()
            ]
        ),
        overlay(),
        AT,
    )
    assert once.facts is not None
    for other in (per_query, in_memory):
        assert other.facts is not None
        assert once.facts.go_given_through == other.facts.go_given_through
        assert once.facts.go_rows == other.facts.go_rows
        assert once.facts.readings == other.facts.readings
        assert once.facts.readings_rejected == other.facts.readings_rejected
        assert once.facts.shadows == other.facts.shadows
        assert once.facts.shadows_rejected == other.facts.shadows_rejected
        assert once.facts.shadows_unread == other.facts.shadows_unread
        assert sorted(once.facts.events, key=event_key) == sorted(
            other.facts.events, key=event_key
        )
    # The recorded rows are read: a parity over empty facts would prove nothing (positive control).
    assert once.facts.events
    assert once.facts.readings
    assert once.facts.go_given_through > 0


def test_a_ledger_that_cannot_be_read_is_still_an_error_and_never_empty(
    tmp_path: Path,
) -> None:
    source = FileLedgerRowSource(tmp_path / "absent.md", tmp_path / "no-archive")
    result = read_ledger_facts(source, overlay(), AT)
    assert result.facts is None
    assert result.error is not None
    assert "go rulings unread" in result.error


def test_the_whole_cycle_over_the_recorded_size_finishes_under_the_bound(
    tmp_path: Path, big_ledger: Path
) -> None:
    home = build_home(tmp_path)
    env = route_env(home, big_ledger)
    handler = HandlerFloorSimpleTick(
        sink=InMemoryEmissionSink(),
        clock=lambda: AT,
        served_alarm_ids=lambda: None,
    )
    started = time.perf_counter()
    result = handler.run(
        ModelFloorSimpleTickRequest(home=str(home), now=AT), environ=env
    )
    elapsed = time.perf_counter() - started
    assert result.ok
    document = ModelFloorSimple.model_validate_json(
        (home / ".claude_scratch/factory-floor/floor-simple.json").read_text(
            encoding="utf-8"
        )
    )
    assert document.sources_read["ledger"] is True
    assert elapsed < CYCLE_BOUND_S, (
        f"the cycle took {elapsed:.1f} s over {LEDGER_ROWS} ledger rows; the bound is {CYCLE_BOUND_S} s "
        "(the publisher's step limit is 15 s)"
    )


def test_the_injected_break_of_one_scan_per_query_fails_the_scan_count(
    big_ledger: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reading can fail for real: a source with no single-pass read is scanned once per query, and the count shows it."""
    opened = count_opens(monkeypatch, big_ledger)
    inner = FileLedgerRowSource(big_ledger, big_ledger.parent / "archive")
    read_ledger_facts(PerQuerySource(inner), overlay(), AT)
    assert max(opened.values()) > 1, opened
