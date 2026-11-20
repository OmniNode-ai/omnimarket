# SPDX-FileCopyrightText: 2026 OmniNode.ai Inc.
# SPDX-License-Identifier: MIT
"""The moved readers of OMN-20548 answer from the bus-fed projection, in budget, never from the file.

Three readers move off ``ROLLING_WORK_LEDGER.md`` here: the inbox check (reader ``ledger-msg-inbox``,
verb ``inbox``), the pause check (reader ``pause-check``, verb ``holds``) and the open-rulings check
(reader ``open-rulings``, verb ``open-rulings``). Each is proved four ways, and the test names are the
falsifier names of the tickets (``source_db``, ``verb``, ``flip_rollback_file``):

(a) the answer is the projection fixture's while the ledger path variable names a missing or a poisoned file;
(b) the read returns inside a time budget on the handler's injected clock (never a sleep), with a
    positive control that a read as slow as the measured one is seen as over budget;
(c) a node fault is ``unreadable`` (exit 69, nothing on stdout, ``source=none``), never an empty answer and
    never CLEAR: the plugin script turns that exit into its own ``source=file-fallback`` line;
(d) the reader taken out of ``moved_readers`` answers not-moved (exit 3) and asks the node nothing, and the
    file the caller then keeps reading still holds its own answer (``flip_rollback_file``).

Budget constants and where they come from (lane ledger-cut-1555, host h053, 2026-11-20, 10 calls each):
the plan's L4 figure is 1 s p95 for ``holds`` and ``inbox`` (section 6a, L4); the pr-handoff budget is 60 s
and the plan states none for ``open-rulings``. The measured node reads miss the 1 s figure: see
``MEASURED_NODE_SERIAL_MEDIAN_S``. The tests prove the budget is checkable, not that the deployed node meets it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from omnimarket.handlers.wip_admission_brief_gate import LEDGER_ENV
from omnimarket.nodes.node_work_ledger_query_effect.handlers.work_ledger_query_reader import (
    WorkLedgerReadError,
)
from omnimarket.nodes.node_work_ledger_reader_effect.adapters import InMemoryEventSink
from omnimarket.nodes.node_work_ledger_reader_effect.handlers import (
    HandlerWorkLedgerReader,
)
from omnimarket.nodes.node_work_ledger_reader_effect.models import (
    EnumReaderQuery,
    EnumReaderStatus,
    ModelPointAnswer,
    ModelPointQuery,
    ModelReaderRequest,
)
from omnimarket.nodes.node_work_ledger_reader_effect.runtime_work_ledger_reader import (
    main,
)
from tests.unit.nodes.node_work_ledger_query_effect.state_fixtures import (
    NOW,
    FakeStateReader,
    hold_entity,
    msg_entity,
    ruling_entity,
)
from tests.unit.nodes.node_work_ledger_reader_effect.test_reader_chain_events import (
    EXAMPLE,
    QueryNodePointRead,
    overlay,
)

pytestmark = pytest.mark.unit

# Plan work-ledger-bus-only-cutover, L4: `holds` and `inbox` at most 1 s p95 over 100 calls.
BUDGET_HOLDS_INBOX_S = 1.0
# The pr-handoff budget; the plan names no figure for open-rulings, so it takes this one.
BUDGET_OPEN_RULINGS_S = 60.0
# report ledger-cut-1555-2026-11-20 line `inbox source=node-serial`: ten serial calls of the node on
# h053, median 6.8 s, max 16.9 s; `pause source=node` median 7.6 s; file `inbox source=file` 0.02 s.
MEASURED_NODE_SERIAL_MEDIAN_S = 6.8
# What one fixture read costs on the injected clock: well inside both budgets.
FIXTURE_READ_S = 0.25

SENTINEL = "FILE-ONLY-SENTINEL-ROW"

CASES = {
    "inbox": ("ledger-msg-inbox", "inbox", {"lane": "lane-x"}, BUDGET_HOLDS_INBOX_S),
    "holds": ("pause-check", "holds", {}, BUDGET_HOLDS_INBOX_S),
    "open-rulings": (
        "open-rulings",
        "open-rulings",
        {"ticket": "OMN-19609"},
        BUDGET_OPEN_RULINGS_S,
    ),
}


def state(case: str) -> FakeStateReader:
    """The projection page the verb reads: the open items of that kind, as the query node folds them."""
    entities = {
        "inbox": (hold_entity(), msg_entity()),
        "holds": (hold_entity(),),
        "open-rulings": (ruling_entity(),),
    }[case]
    return FakeStateReader(entities)


class SteppedClock:
    """A clock that moves only when the fake node read costs time."""

    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class TimedPointRead:
    """The query node in process; every read advances the injected clock by its cost."""

    def __init__(
        self, reader: FakeStateReader, clock: SteppedClock, cost_s: float
    ) -> None:
        self._inner = QueryNodePointRead(reader)
        self._clock = clock
        self._cost = timedelta(seconds=cost_s)
        self.requests: list[ModelPointQuery] = self._inner.requests

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        answer = self._inner.read(query)
        self._clock.now += self._cost
        return answer


class FreshPointRead:
    """The same projection fixture, its watermark stamped now so the CLI's real clock reads it fresh."""

    def __init__(self, reader: FakeStateReader) -> None:
        self._inner = QueryNodePointRead(reader)
        self.requests: list[ModelPointQuery] = self._inner.requests

    def read(self, query: ModelPointQuery) -> ModelPointAnswer:
        answer = self._inner.read(query)
        return answer.model_copy(update={"watermark": datetime.now(UTC)})


def overlay_file(tmp_path: Path, moved: tuple[str, ...]) -> Path:
    document = yaml.safe_load(EXAMPLE.read_text(encoding="utf-8"))
    document["moved_readers"] = list(moved)
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def argv(case: str) -> list[str]:
    reader, query, extra, _ = CASES[case]
    out = ["--reader", reader, "--query", query]
    for key, value in extra.items():
        out += [f"--{key}", value]
    return out


def request(case: str) -> ModelReaderRequest:
    reader, query, extra, _ = CASES[case]
    return ModelReaderRequest(reader=reader, query=EnumReaderQuery(query), **extra)


def handler(
    case: str, read: object, clock: SteppedClock | None = None
) -> HandlerWorkLedgerReader:
    reader = CASES[case][0]
    return HandlerWorkLedgerReader(
        overlay(moved_readers=(reader,)),
        read,  # type: ignore[arg-type]
        InMemoryEventSink(),
        clock=clock or (lambda: NOW),
    )


@pytest.mark.parametrize("poison", ["missing", "poisoned"])
@pytest.mark.parametrize("case", sorted(CASES))
def test_source_db_verb_answers_from_projection_not_the_file(
    case: str, poison: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    if poison == "poisoned":
        ledger.write_text(f"{SENTINEL}\nnot | a | row\n", encoding="utf-8")
    env = {
        "ONEX_LEDGER_READER_OVERLAY": str(overlay_file(tmp_path, (CASES[case][0],))),
        LEDGER_ENV: str(ledger),
    }
    read = FreshPointRead(state(case))
    code = main(argv(case), environ=env, point_read=read)
    out = capsys.readouterr()
    assert code == 0
    assert "status=ok source=node" in out.err
    assert "file_read=no" in out.err
    assert SENTINEL not in out.out
    assert out.out.strip() != ""
    assert [r.query for r in read.requests] == [CASES[case][1]]
    assert ledger.exists() is (poison == "poisoned")


@pytest.mark.parametrize("case", sorted(CASES))
def test_source_db_verb_returns_within_budget_on_the_injected_clock(
    case: str,
) -> None:
    budget = CASES[case][3]
    clock = SteppedClock()
    read = TimedPointRead(state(case), clock, FIXTURE_READ_S)
    result = handler(case, read, clock).handle(request(case))
    assert result.status is EnumReaderStatus.OK
    assert result.elapsed_s is not None
    assert result.elapsed_s == pytest.approx(FIXTURE_READ_S)
    assert result.elapsed_s <= budget
    assert f"elapsed_s={result.elapsed_s:.3f}" in result.summary()


@pytest.mark.parametrize("case", ["inbox", "holds"])
def test_source_db_verb_budget_is_seen_missed_at_the_measured_latency(
    case: str,
) -> None:
    """Positive control: a read as slow as the 2026-11-20 measurement is over the 1 s plan figure."""
    clock = SteppedClock()
    read = TimedPointRead(state(case), clock, MEASURED_NODE_SERIAL_MEDIAN_S)
    result = handler(case, read, clock).handle(request(case))
    assert result.status is EnumReaderStatus.OK
    assert result.elapsed_s is not None
    assert result.elapsed_s > BUDGET_HOLDS_INBOX_S


@pytest.mark.parametrize("case", sorted(CASES))
def test_source_db_verb_node_fault_is_unreadable_never_empty_or_clear(
    case: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    env = {
        "ONEX_LEDGER_READER_OVERLAY": str(overlay_file(tmp_path, (CASES[case][0],))),
        LEDGER_ENV: str(tmp_path / "ROLLING_WORK_LEDGER.md"),
    }
    read = FreshPointRead(FakeStateReader(error=WorkLedgerReadError("db down")))
    code = main(argv(case), environ=env, point_read=read)
    out = capsys.readouterr()
    assert code == 69
    assert out.out == ""
    assert "status=unreadable source=none" in out.err
    assert "file_read=no" in out.err


@pytest.mark.parametrize("case", sorted(CASES))
def test_flip_rollback_file_reader_not_moved_asks_the_node_nothing(
    case: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger = tmp_path / "ROLLING_WORK_LEDGER.md"
    ledger.write_text(f"{SENTINEL}\n", encoding="utf-8")
    reader = CASES[case][0]
    read = FreshPointRead(state(case))

    moved = {
        "ONEX_LEDGER_READER_OVERLAY": str(overlay_file(tmp_path, (reader,))),
        LEDGER_ENV: str(ledger),
    }
    assert main(argv(case), environ=moved, point_read=read) == 0
    capsys.readouterr()
    assert len(read.requests) == 1

    rolled_back = {
        "ONEX_LEDGER_READER_OVERLAY": str(overlay_file(tmp_path, ("drain-map",))),
        LEDGER_ENV: str(ledger),
    }
    code = main(argv(case), environ=rolled_back, point_read=read)
    out = capsys.readouterr()
    assert code == 3
    assert out.out == ""
    assert "status=not-moved" in out.err
    assert "elapsed_s=none" in out.err
    assert len(read.requests) == 1
    # The caller keeps its own read: the file still holds its own answer, untouched.
    assert ledger.read_text(encoding="utf-8") == f"{SENTINEL}\n"
